import copy
import json
import unittest

import probe


def _responses_wire(*events):
    normalized = []
    for event in events:
        event = copy.deepcopy(event)
        if event["type"] in {"response.created", "response.in_progress", "response.completed", "response.done"} and isinstance(event.get("response"), dict):
            event["response"].setdefault("id", "resp_1")
            event["response"].setdefault("object", "response")
            if event["type"] == "response.created":
                event["response"].setdefault("status", "in_progress")
                event["response"].setdefault("output", [])
        normalized.append(event)
    return [
        {"kind": "event", "sequence": index, "wire_sequence": index, "type": event["type"], "event": event}
        for index, event in enumerate(normalized)
    ]


def _response_message_stream():
    message = {"id": "msg_1", "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "answer"}]}
    return _responses_wire(
        {"type": "response.created", "response": {}},
        {"type": "response.output_item.added", "output_index": 0, "item": {"id": "msg_1", "type": "message", "role": "assistant"}},
        {
            "type": "response.content_part.added",
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": ""},
        },
        {"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 0, "content_index": 0, "delta": "answer"},
        {"type": "response.output_text.done", "item_id": "msg_1", "output_index": 0, "content_index": 0, "text": "answer"},
        {
            "type": "response.content_part.done",
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "answer"},
        },
        {"type": "response.output_item.done", "output_index": 0, "item": message},
        {"type": "response.completed", "response": {"status": "completed", "output": [message]}},
    )


def _response_multi_content_stream():
    message = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "output_text", "text": "first"},
            {"type": "output_text", "text": "second"},
        ],
    }
    return _responses_wire(
        {"type": "response.created", "response": {}},
        {"type": "response.output_item.added", "output_index": 0, "item": {"id": "msg_1", "type": "message", "role": "assistant"}},
        {
            "type": "response.content_part.added",
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": ""},
        },
        {"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 0, "content_index": 0, "delta": "first"},
        {"type": "response.output_text.done", "item_id": "msg_1", "output_index": 0, "content_index": 0, "text": "first"},
        {
            "type": "response.content_part.done",
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "first"},
        },
        {
            "type": "response.content_part.added",
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 1,
            "part": {"type": "output_text", "text": ""},
        },
        {"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 0, "content_index": 1, "delta": "second"},
        {"type": "response.output_text.done", "item_id": "msg_1", "output_index": 0, "content_index": 1, "text": "second"},
        {
            "type": "response.content_part.done",
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 1,
            "part": {"type": "output_text", "text": "second"},
        },
        {"type": "response.output_item.done", "output_index": 0, "item": message},
        {"type": "response.completed", "response": {"status": "completed", "output": [message]}},
    )


def _response_reasoning_stream():
    reasoning = {"id": "rs_1", "type": "reasoning", "summary": [{"type": "summary_text", "text": "thought"}]}
    return _responses_wire(
        {"type": "response.created", "response": {}},
        {"type": "response.output_item.added", "output_index": 0, "item": {"id": "rs_1", "type": "reasoning"}},
        {"type": "response.reasoning_summary_text.delta", "item_id": "rs_1", "output_index": 0, "summary_index": 0, "delta": "thought"},
        {"type": "response.reasoning_summary_text.done", "item_id": "rs_1", "output_index": 0, "summary_index": 0, "text": "thought"},
        {"type": "response.output_item.done", "output_index": 0, "item": reasoning},
        {"type": "response.completed", "response": {"status": "completed", "output": [reasoning]}},
    )


def _response_multi_summary_stream():
    reasoning = {
        "id": "rs_1",
        "type": "reasoning",
        "summary": [
            {"type": "summary_text", "text": "first"},
            {"type": "summary_text", "text": "second"},
        ],
    }
    return _responses_wire(
        {"type": "response.created", "response": {}},
        {"type": "response.output_item.added", "output_index": 0, "item": {"id": "rs_1", "type": "reasoning"}},
        {"type": "response.reasoning_summary_text.delta", "output_index": 0, "item_id": "rs_1", "summary_index": 0, "delta": "first"},
        {"type": "response.reasoning_summary_text.done", "output_index": 0, "item_id": "rs_1", "summary_index": 0, "text": "first"},
        {"type": "response.reasoning_summary_text.delta", "output_index": 0, "item_id": "rs_1", "summary_index": 1, "delta": "second"},
        {"type": "response.reasoning_summary_text.done", "output_index": 0, "item_id": "rs_1", "summary_index": 1, "text": "second"},
        {"type": "response.output_item.done", "output_index": 0, "item": reasoning},
        {"type": "response.completed", "response": {"status": "completed", "output": [reasoning]}},
    )


def _response_function_stream():
    function_call = {
        "id": "fc_1",
        "type": "function_call",
        "call_id": "call_1",
        "name": "lookup_weather",
        "arguments": '{"city":"Shanghai"}',
    }
    return _responses_wire(
        {"type": "response.created", "response": {}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "id": "fc_1",
                "type": "function_call",
                "call_id": "call_1",
                "name": "lookup_weather",
                "arguments": "",
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_1",
            "output_index": 0,
            "delta": '{"city":"Shanghai"}',
        },
        {
            "type": "response.function_call_arguments.done",
            "item_id": "fc_1",
            "output_index": 0,
            "arguments": '{"city":"Shanghai"}',
        },
        {"type": "response.output_item.done", "output_index": 0, "item": function_call},
        {"type": "response.completed", "response": {"status": "completed", "output": [function_call]}},
    )


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
                {"kind": "event", "type": "response.function_call_arguments.done", "event": {"type": "response.function_call_arguments.done", "item_id": "fc_1", "arguments": '{"city":"Shanghai"}'}},
                {"kind": "event", "type": "response.output_item.done", "event": {"type": "response.output_item.done", "item": {"id": "fc_1", "type": "function_call", "call_id": "call_1", "name": "lookup_weather", "arguments": '{"city":"Shanghai"}'}}},
                {"kind": "event", "type": "response.completed", "event": {"type": "response.completed", "response": {"status": "completed", "output": [{"id": "fc_1", "type": "function_call", "call_id": "call_1", "name": "lookup_weather", "arguments": '{"city":"Shanghai"}'}]}}},
            ],
            True,
            0,
        )
        turn = probe._parse_responses_stream(result)
        self.assertEqual(turn.tool_calls[0].arguments, {"city": "Shanghai"})
        self.assertTrue(turn.terminal)

    def test_responses_stream_reassembles_multiple_reasoning_summaries(self) -> None:
        events = _response_multi_summary_stream()
        self.assertTrue(probe._stream_order_ok("responses", events))
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, events, False, 0))
        self.assertNotIn("stream_text_done_mismatch", turn.parse_errors)
        self.assertEqual(turn.reasoning_text, "firstsecond")

    def test_responses_stream_requires_explicit_reasoning_summary_indexes(self) -> None:
        events = _response_reasoning_stream()
        events[2]["event"].pop("summary_index")
        events[3]["event"].pop("summary_index")
        self.assertFalse(probe._stream_order_ok("responses", events))
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, events, False, 0))
        self.assertIn("summary_index_invalid", turn.parse_errors)

    def test_responses_stream_requires_reasoning_summary_lifecycle(self) -> None:
        opening = _response_reasoning_stream()
        opening[1]["event"]["item"]["summary"] = [{"type": "summary_text", "text": "eager"}]
        self.assertFalse(probe._stream_order_ok("responses", opening))
        opening_turn = probe._parse_responses_stream(probe.TransportResult(200, None, opening, False, 0))
        self.assertIn("stream_reasoning_opening_snapshot_invalid", opening_turn.parse_errors)

        missing = [
            event
            for event in _response_reasoning_stream()
            if event["type"] not in {"response.reasoning_summary_text.delta", "response.reasoning_summary_text.done"}
        ]
        self.assertFalse(probe._stream_order_ok("responses", missing))
        missing_turn = probe._parse_responses_stream(probe.TransportResult(200, None, missing, False, 0))
        self.assertIn("stream_reasoning_summary_events_missing", missing_turn.parse_errors)

    def test_responses_stream_rejects_orphaned_reasoning_summary_done(self) -> None:
        events = _response_reasoning_stream()
        events.insert(
            4,
            {
                "kind": "event",
                "type": "response.reasoning_summary_text.done",
                "event": {
                    "type": "response.reasoning_summary_text.done",
                    "item_id": "rs_1",
                    "output_index": 0,
                    "summary_index": 1,
                    "text": "",
                },
            },
        )
        for sequence, event in enumerate(events):
            event["sequence"] = sequence
            event["wire_sequence"] = sequence
        self.assertFalse(probe._stream_order_ok("responses", events))
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, events, False, 0))
        self.assertIn("stream_reasoning_summary_done_without_delta", turn.parse_errors)

    def test_responses_reasoning_opening_content_must_be_empty(self) -> None:
        valid = _response_reasoning_stream()
        self.assertTrue(probe._stream_order_ok("responses", valid))
        for opening_content in ([{"type": "summary_text", "text": "prefilled"}], {"unexpected": True}):
            invalid = copy.deepcopy(valid)
            invalid[1]["event"]["item"]["content"] = opening_content
            self.assertFalse(probe._stream_order_ok("responses", invalid))
            turn = probe._parse_responses_stream(probe.TransportResult(200, None, invalid, False, 0))
            self.assertIn("stream_reasoning_opening_snapshot_invalid", turn.parse_errors)

        empty = copy.deepcopy(valid)
        empty[1]["event"]["item"]["content"] = []
        self.assertTrue(probe._stream_order_ok("responses", empty))

    def test_responses_reasoning_summary_snapshots_match_each_delta_index(self) -> None:
        invalid = _response_multi_summary_stream()
        invalid[6]["event"]["item"]["summary"] = [
            {"type": "summary_text", "text": "firsts"},
            {"type": "summary_text", "text": "econd"},
        ]
        self.assertFalse(probe._stream_order_ok("responses", invalid))
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, invalid, False, 0))
        self.assertIn("stream_reasoning_summary_snapshot_mismatch", turn.parse_errors)

    def test_responses_stream_rejects_noncontiguous_reasoning_summary_index(self) -> None:
        events = _response_multi_summary_stream()
        events[4]["event"]["summary_index"] = 2
        events[5]["event"]["summary_index"] = 2
        self.assertFalse(probe._stream_order_ok("responses", events))
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, events, False, 0))
        self.assertIn("summary_index_invalid", turn.parse_errors)

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

    def test_responses_stream_rejects_argument_done_mismatch(self) -> None:
        events = [
            {"kind": "event", "sequence": 0, "wire_sequence": 0, "type": "response.created", "event": {"type": "response.created", "response": {"id": "resp_1", "object": "response", "status": "in_progress", "output": []}}},
            {"kind": "event", "sequence": 1, "wire_sequence": 1, "type": "response.output_item.added", "event": {"type": "response.output_item.added", "output_index": 0, "item": {"id": "fc_1", "type": "function_call", "call_id": "call_1", "name": "lookup_weather"}}},
            {"kind": "event", "sequence": 2, "wire_sequence": 2, "type": "response.function_call_arguments.delta", "event": {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "output_index": 0, "delta": '{"city":"Shanghai"}'}},
            {"kind": "event", "sequence": 3, "wire_sequence": 3, "type": "response.function_call_arguments.done", "event": {"type": "response.function_call_arguments.done", "item_id": "fc_1", "output_index": 0, "arguments": '{"city":"Paris"}'}},
            {"kind": "event", "sequence": 4, "wire_sequence": 4, "type": "response.output_item.done", "event": {"type": "response.output_item.done", "output_index": 0, "item": {"id": "fc_1", "type": "function_call", "call_id": "call_1", "name": "lookup_weather", "arguments": '{"city":"Shanghai"}'}}},
            {"kind": "event", "sequence": 5, "wire_sequence": 5, "type": "response.completed", "event": {"type": "response.completed", "response": {"id": "resp_1", "object": "response", "status": "completed", "output": [{"id": "fc_1", "type": "function_call", "call_id": "call_1", "name": "lookup_weather", "arguments": '{"city":"Shanghai"}' }]}}},
        ]
        self.assertTrue(probe._stream_order_ok("responses", events))
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, events, False, 0, True))
        self.assertIn("stream_arguments_done_mismatch", turn.parse_errors)

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
            {"kind": "event", "type": None, "event": {"object": "chat.completion.chunk", "choices": [{"delta": {"tool_calls": [{"index": 0, "type": "function", "id": "call_1", "function": {"name": "lookup_weather", "arguments": '{"city":'}}]}}]}},
            {"kind": "event", "type": None, "event": {"object": "chat.completion.chunk", "choices": [{"delta": {"tool_calls": [{"index": 0, "type": "function", "function": {"arguments": '"Shanghai"}'}}]}}]}},
        ]
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, events, True, 0))
        self.assertEqual(turn.tool_calls[0].arguments, {"city": "Shanghai"})
        self.assertTrue(turn.terminal)

    def test_chat_stream_rejects_falsey_malformed_tool_fields(self) -> None:
        events = [
            {"kind": "event", "type": None, "event": {"object": "chat.completion.chunk", "choices": [{"delta": {"role": "assistant", "tool_calls": [{"index": 0, "type": "function", "id": "call_1", "function": {"name": "lookup_weather", "arguments": "{"}}]}}]}},
            {"kind": "event", "type": None, "event": {"object": "chat.completion.chunk", "choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": [], "arguments": []}}]}, "finish_reason": "tool_calls"}]}},
        ]
        self.assertFalse(probe._stream_order_ok("chat", events + [{"kind": "done", "type": None}]))
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, events, True, 0))
        self.assertIn("stream_name_invalid", turn.parse_errors)
        self.assertIn("stream_arguments_invalid", turn.parse_errors)

    def test_chat_stream_allows_sparse_continuation_type(self) -> None:
        events = [
            {"kind": "event", "type": None, "event": {"object": "chat.completion.chunk", "choices": [{"delta": {"tool_calls": [{"index": 0, "type": "function", "id": "call_1", "function": {"arguments": '{"city":'}}]}}]}},
            {"kind": "event", "type": None, "event": {"object": "chat.completion.chunk", "choices": [{"delta": {"tool_calls": [{"index": 0, "type": None, "function": {"arguments": '"Shanghai"}'}}]}}]}},
        ]
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, events, True, 0))
        self.assertEqual(turn.tool_calls[0].arguments, {"city": "Shanghai"})
        self.assertNotIn("tool_call_type_invalid", turn.parse_errors)

    def test_chat_stream_requires_argument_fragments(self) -> None:
        events = [
            {"kind": "event", "type": None, "event": {"object": "chat.completion.chunk", "choices": [{"delta": {"tool_calls": [{"index": 0, "type": "function", "id": "call_1", "function": {"name": "lookup_weather"}}]}}]}},
            {"kind": "event", "type": None, "event": {"object": "chat.completion.chunk", "choices": [{"delta": {}, "finish_reason": "tool_calls"}]}},
        ]
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, events, True, 0))
        self.assertIn("stream_arguments_missing", turn.parse_errors)

    def test_chat_stream_rejects_tool_call_id_changes(self) -> None:
        events = [
            {"kind": "event", "type": None, "event": {"object": "chat.completion.chunk", "choices": [{"delta": {"tool_calls": [{"index": 0, "type": "function", "id": "call_1", "function": {"name": "lookup_weather", "arguments": '{"city":"Shanghai"}'}}]}}]}},
            {"kind": "event", "type": None, "event": {"object": "chat.completion.chunk", "choices": [{"delta": {"tool_calls": [{"index": 0, "type": "function", "id": "call_2", "function": {}}]}}]}},
            {"kind": "event", "type": None, "event": {"object": "chat.completion.chunk", "choices": [{"delta": {}, "finish_reason": "tool_calls"}]}},
        ]
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, events, True, 0))
        self.assertIn("tool_call_id_changed", turn.parse_errors)
        self.assertEqual(turn.tool_calls[0].call_id, "call_1")

    def test_chat_stream_requires_tool_call_id_on_opening_fragment(self) -> None:
        events = [
            {
                "kind": "event",
                "sequence": 0,
                "event": {
                    "id": "chatcmpl_1",
                    "object": "chat.completion.chunk",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "type": "function",
                                        "function": {"name": "lookup_weather", "arguments": '{"city":'},
                                    }
                                ],
                            },
                        }
                    ],
                },
            },
            {
                "kind": "event",
                "sequence": 1,
                "event": {
                    "id": "chatcmpl_1",
                    "object": "chat.completion.chunk",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {"index": 0, "id": "call_1", "function": {"arguments": '"Shanghai"}'}}
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                },
            },
            {"kind": "done", "sequence": 2},
        ]
        self.assertFalse(probe._stream_order_ok("chat", events))
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, events, True, 0))
        self.assertIn("tool_call_id_invalid", turn.parse_errors)

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

    def test_tool_result_tuple_check_rejects_extra_tuple(self) -> None:
        calls = [
            probe.ToolCall("call_weather", "lookup_weather", {"city": "Shanghai"}),
            probe.ToolCall("call_time", "lookup_time", {"city": "Shanghai"}),
        ]
        text = " ".join(
            [
                probe._tool_output(calls[0]),
                probe._tool_output(calls[1]),
                "tool=lookup_weather;call_id=fabricated;marker=WEATHER_OK",
            ]
        )
        turn = probe._parse_anthropic_document({"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"})
        projection = probe._validate_second(turn, ("lookup_weather", "lookup_time"), stream=False, expected_calls=calls)
        self.assertFalse(projection["checks"]["tool_output_call_pairs"])

    def test_tool_result_tuple_check_rejects_unrecognized_marker(self) -> None:
        call = probe.ToolCall("call_weather", "lookup_weather", {"city": "Shanghai"})
        text = f"{probe._tool_output(call)} tool=lookup_weather;call_id=fake;marker=BOGUS."
        self.assertEqual(
            probe._tool_output_tuples(text),
            [
                ("lookup_weather", "call_weather", "WEATHER_OK"),
                ("lookup_weather", "fake", "BOGUS"),
            ],
        )
        turn = probe._parse_anthropic_document(
            {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}
        )
        projection = probe._validate_second(turn, ("lookup_weather",), stream=False, expected_calls=[call])
        self.assertFalse(projection["checks"]["tool_output_call_pairs"])

    def test_tool_result_tuple_preserves_punctuation_in_call_id(self) -> None:
        call = probe.ToolCall("call.123", "lookup_weather", {"city": "Shanghai"})
        text = f"{probe._tool_output(call)}."
        self.assertEqual(probe._tool_output_tuples(text), [("lookup_weather", "call.123", "WEATHER_OK")])
        turn = probe._parse_anthropic_document({"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"})
        projection = probe._validate_second(turn, ("lookup_weather",), stream=False, expected_calls=[call])
        self.assertTrue(projection["checks"]["tool_output_call_pairs"])

    def test_empty_responses_reasoning_item_is_not_a_signal(self) -> None:
        turn = probe._parse_responses_document(
            {"output": [{"id": "rs_1", "type": "reasoning", "summary": [], "content": []}, {"id": "fc_1", "type": "function_call", "call_id": "call_1", "name": "lookup_weather", "arguments": '{"city":"Shanghai"}'}], "status": "completed"}
        )
        self.assertFalse(turn.reasoning_present)

    def test_responses_encrypted_reasoning_is_a_signal(self) -> None:
        turn = probe._parse_responses_document(
            {"output": [{"id": "rs_1", "type": "reasoning", "encrypted_content": "opaque"}], "status": "completed"}
        )
        self.assertTrue(turn.reasoning_present)

    def test_responses_encrypted_reasoning_requires_string_payload(self) -> None:
        turn = probe._parse_responses_document(
            {"output": [{"id": "rs_1", "type": "reasoning", "encrypted_content": True}], "status": "completed"}
        )
        self.assertFalse(turn.reasoning_present)

    def test_responses_reasoning_parts_require_supported_string_payloads(self) -> None:
        for part, error in (
            ({"type": "future_part", "text": "ignored"}, "reasoning_part_type_invalid"),
            ({"type": "summary_text", "text": []}, "reasoning_part_text_invalid"),
        ):
            turn = probe._parse_responses_document(
                {"object": "response", "output": [{"id": "rs_1", "type": "reasoning", "summary": [part]}], "status": "completed"}
            )
            self.assertIn(error, turn.parse_errors)

    def test_responses_nonstream_output_items_require_ids(self) -> None:
        for item in (
            {"type": "function_call", "call_id": "call_1", "name": "lookup_weather", "arguments": '{"city":"Shanghai"}'},
            {"id": [], "type": "function_call", "call_id": "call_1", "name": "lookup_weather", "arguments": '{"city":"Shanghai"}'},
            {"id": "", "type": "function_call", "call_id": "call_1", "name": "lookup_weather", "arguments": '{"city":"Shanghai"}'},
        ):
            turn = probe._parse_responses_document({"object": "response", "output": [item], "status": "completed"})
            self.assertIn("output_item_id_invalid", turn.parse_errors)

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
            {"kind": "event", "type": None, "event": {"object": "chat.completion.chunk", "choices": [{"delta": {}, "finish_reason": "tool_calls"}]}},
        ]
        self.assertFalse(probe._stream_order_ok("chat", events))

    def test_chat_stream_done_sentinel_is_not_an_envelope(self) -> None:
        events = [
            {"kind": "event", "type": None, "event": {"object": "chat.completion.chunk", "choices": [{"delta": {"role": "assistant"}, "finish_reason": "stop"}]}},
            {"kind": "done", "type": None},
        ]
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, events, True, 0))
        self.assertNotIn("stream_envelope_invalid", turn.parse_errors)
        self.assertTrue(turn.terminal)

    def test_chat_stream_rejects_named_done_sentinel(self) -> None:
        events = [
            {"kind": "event", "type": None, "event": {"object": "chat.completion.chunk", "choices": [{"delta": {"role": "assistant"}, "finish_reason": "stop"}]}},
            {"kind": "done", "type": "bogus"},
        ]
        self.assertFalse(probe._stream_order_ok("chat", events))
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, events, True, 0))
        self.assertIn("stream_done_event_type_invalid", turn.parse_errors)

    def test_chat_stream_requires_chunk_discriminator(self) -> None:
        events = [
            {"kind": "event", "type": None, "event": {"choices": [{"delta": {"role": "assistant"}, "finish_reason": "stop"}]}},
            {"kind": "done", "type": None},
        ]
        self.assertFalse(probe._stream_order_ok("chat", events))
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, events, True, 0))
        self.assertIn("stream_object_invalid", turn.parse_errors)

    def test_chat_stream_rejects_unhashable_continuation_type(self) -> None:
        events = [
            {"kind": "event", "type": None, "event": {"object": "chat.completion.chunk", "choices": [{"delta": {"role": "assistant", "tool_calls": [{"index": 0, "type": "function", "id": "call_1", "function": {"name": "lookup_weather", "arguments": "{\"city\":\"Shanghai\"}"}}]}}]}},
            {"kind": "event", "type": None, "event": {"object": "chat.completion.chunk", "choices": [{"delta": {"tool_calls": [{"index": 0, "type": [], "function": {}}]}, "finish_reason": "tool_calls"}]}},
            {"kind": "done", "type": None},
        ]
        self.assertFalse(probe._stream_order_ok("chat", events))
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, events, True, 0))
        self.assertIn("tool_call_type_invalid", turn.parse_errors)

    def test_chat_stream_rejects_non_object_tool_fragments(self) -> None:
        events = [
            {"kind": "event", "sequence": 0, "type": None, "event": {"object": "chat.completion.chunk", "choices": [{"delta": {"role": "assistant", "tool_calls": [None]}, "finish_reason": "tool_calls"}]}},
            {"kind": "done", "sequence": 1, "type": None},
        ]
        self.assertFalse(probe._stream_order_ok("chat", events))
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, events, True, 0, False))
        self.assertIn("tool_call_invalid", turn.parse_errors)

    def test_chat_stream_allows_indices_to_restart_each_chunk(self) -> None:
        events = [
            {"kind": "event", "sequence": 0, "event": {"id": "chatcmpl_1", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_1"}, {"index": 1, "id": "call_2"}]}}]}},
            {"kind": "event", "sequence": 1, "event": {"id": "chatcmpl_1", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0}, {"index": 1}], "content": "done"}, "finish_reason": "tool_calls"}]}},
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

    def test_sse_parser_normalizes_default_event_names(self) -> None:
        payload = '{"id":"chatcmpl_1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":"stop"}]}'
        for event_name in ("message", ""):
            events: list[dict[str, object]] = []
            invalid = [0]
            probe._flush_sse([payload], event_name, events, invalid)
            probe._flush_sse(["[DONE]"], event_name, events, invalid)
            self.assertEqual([event["type"] for event in events], [None, None])
            self.assertEqual(invalid, [0])
            self.assertTrue(probe._stream_order_ok("chat", events))

    def test_sse_parser_rejects_trailing_event_name_whitespace(self) -> None:
        payload = (
            b'event: message_start \ndata: {"type":"message_start","message":{"type":"message",'
            b'"role":"assistant","content":[],"stop_reason":null,"stop_sequence":null}}\n\n'
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
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
        self.assertFalse(result.stream_order_ok)

    def test_sse_parser_preserves_extra_data_whitespace(self) -> None:
        payload = b"data:  [DONE]\n\n"

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
            result = probe._request("/v1/chat/completions", {}, client_protocol="chat", stream=True)
        finally:
            probe.OPENER = original_opener
        self.assertFalse(result.done_sentinel)
        self.assertEqual(result.invalid_event_count, 1)

    def test_sse_parser_preserves_bare_data_field(self) -> None:
        payload = b"data: [DONE]\ndata\n\n"

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
            result = probe._request("/v1/chat/completions", {}, client_protocol="chat", stream=True)
        finally:
            probe.OPENER = original_opener
        self.assertFalse(result.done_sentinel)
        self.assertEqual(result.invalid_event_count, 1)

    def test_sse_parser_discards_unterminated_event_at_eof(self) -> None:
        for payload in (b"data: [DONE]\n", b"data: [DONE]"):
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
                result = probe._request("/v1/chat/completions", {}, client_protocol="chat", stream=True)
            finally:
                probe.OPENER = original_opener
            self.assertFalse(result.done_sentinel)
            self.assertEqual(result.events, [])
            self.assertFalse(result.stream_order_ok)

    def test_chat_done_sentinel_ends_read_before_transport_eof(self) -> None:
        payload = (
            b'data: {"id":"chatcmpl_1","object":"chat.completion.chunk","choices":'
            b'[{"index":0,"delta":{"role":"assistant"},"finish_reason":"stop"}]}\n\n'
            b"data: [DONE]\n\n"
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
                if self.offset >= len(payload):
                    raise TimeoutError
                chunk = payload[self.offset : self.offset + size]
                self.offset += len(chunk)
                return chunk

        class Opener:
            def open(self, request, timeout):
                return Response()

        original_opener = probe.OPENER
        try:
            probe.OPENER = Opener()
            result = probe._request("/v1/chat/completions", {}, client_protocol="chat", stream=True)
        finally:
            probe.OPENER = original_opener
        self.assertTrue(result.done_sentinel)
        self.assertFalse(result.deadline_expired)
        self.assertTrue(result.stream_order_ok)

    def test_chat_stream_rejects_non_function_tool_type(self) -> None:
        events = [
            {"kind": "event", "type": None, "event": {"object": "chat.completion.chunk", "choices": [{"delta": {"tool_calls": [{"index": 0, "type": "custom", "id": "call_1", "function": {"name": "lookup_weather", "arguments": '{"city":"Shanghai"}'}}]}}]}},
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

    def test_anthropic_response_rejects_unknown_content_block_types(self) -> None:
        turn = probe._parse_anthropic_document(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "future_block", "payload": "ignored"}],
                "stop_reason": "end_turn",
            }
        )
        self.assertIn("content_block_type_invalid", turn.parse_errors)

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

    def test_nonstream_rejects_duplicate_outer_json_keys(self) -> None:
        class Response:
            status = 200
            headers: dict[str, str] = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self) -> bytes:
                return b'{"type":"message","role":"assistant","content":[{"type":"tool_use","input":{"city":"Paris","city":"Shanghai"}}]}'

        class Opener:
            def open(self, request, timeout):
                return Response()

        original_opener = probe.OPENER
        try:
            probe.OPENER = Opener()
            result = probe._request("/v1/messages", {}, client_protocol="anthropic", stream=False)
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
            b'\xef\xbb\xbfevent: message_start\rdata: {"type":"message_start","message":'
            b'{"type":"message","role":"assistant","content":[],"stop_reason":null,"stop_sequence":null}}\r\r'
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

    def test_chat_reasoning_content_requires_nonempty_string(self) -> None:
        for value in (True, 1, {}, []):
            turn = probe._parse_chat_document(
                {
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "reasoning_content": value},
                            "finish_reason": "stop",
                        }
                    ]
                }
            )
            self.assertFalse(turn.reasoning_present)
            self.assertIn("reasoning_content_invalid", turn.parse_errors)

    def test_chat_message_content_requires_string_or_null(self) -> None:
        for value in ({"text": "CPA_SYSTEM_MARKER_731"}, ["WEATHER_OK"]):
            turn = probe._parse_chat_document(
                {
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": value},
                            "finish_reason": "stop",
                        }
                    ]
                }
            )
            self.assertIn("message_content_invalid", turn.parse_errors)

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

    def test_responses_message_items_require_assistant_list_content(self) -> None:
        invalid_content = probe._parse_responses_document(
            {"output": [{"type": "message", "role": "assistant", "content": None}], "status": "completed"}
        )
        invalid_role = probe._parse_responses_document(
            {"output": [{"type": "message", "role": "user", "content": []}], "status": "completed"}
        )
        self.assertIn("message_content_invalid", invalid_content.parse_errors)
        self.assertIn("assistant_role_invalid", invalid_role.parse_errors)

    def test_malformed_stream_indexes_fail_without_parser_exceptions(self) -> None:
        anthropic_events = [
            {"kind": "event", "sequence": 0, "event": {"type": "content_block_start", "index": None, "content_block": {"type": "tool_use", "id": "call_1", "name": "lookup_weather"}}},
            {"kind": "event", "sequence": 1, "event": {"type": "message_stop"}},
        ]
        chat_events = [
            {"kind": "event", "sequence": 0, "event": {"object": "chat.completion.chunk", "choices": [{"delta": {"tool_calls": [{"index": None, "id": "call_1", "function": {"name": "lookup_weather", "arguments": '{"city":"Shanghai"}'}}]}}]}},
            {"kind": "event", "sequence": 1, "event": {"object": "chat.completion.chunk", "choices": [{"delta": {}, "finish_reason": "tool_calls"}]}},
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
            {
                "kind": "event",
                "sequence": 0,
                "type": "message_start",
                "event": {
                    "type": "message_start",
                    "message": {
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                    },
                },
            },
            {"kind": "event", "sequence": 1, "type": "content_block_start", "event": {"type": "content_block_start", "index": 0}},
            {"kind": "event", "sequence": 2, "type": "content_block_delta", "event": {"type": "content_block_delta", "index": 0}},
            {"kind": "event", "sequence": 3, "type": "content_block_stop", "event": {"type": "content_block_stop", "index": 0}},
            {"kind": "event", "sequence": 4, "type": "message_stop", "event": {"type": "message_stop"}},
        ]
        invalid = [ordered[2], ordered[0], ordered[3], ordered[4]]
        self.assertTrue(probe._stream_order_ok("anthropic", ordered))
        self.assertFalse(probe._stream_order_ok("anthropic", invalid))

    def test_anthropic_stream_rejects_overlapping_content_blocks(self) -> None:
        events = [
            {"kind": "event", "sequence": 0, "type": "message_start", "event": {"type": "message_start", "message": {"type": "message", "role": "assistant", "content": [], "stop_reason": None, "stop_sequence": None}}},
            {"kind": "event", "sequence": 1, "type": "content_block_start", "event": {"type": "content_block_start", "index": 0}},
            {"kind": "event", "sequence": 2, "type": "content_block_start", "event": {"type": "content_block_start", "index": 1}},
            {"kind": "event", "sequence": 3, "type": "content_block_stop", "event": {"type": "content_block_stop", "index": 0}},
            {"kind": "event", "sequence": 4, "type": "content_block_stop", "event": {"type": "content_block_stop", "index": 1}},
            {"kind": "event", "sequence": 5, "type": "message_stop", "event": {"type": "message_stop"}},
        ]
        self.assertFalse(probe._stream_order_ok("anthropic", events))

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

    def test_anthropic_stream_rejects_unknown_event_types(self) -> None:
        events = [
            {
                "kind": "event",
                "sequence": 0,
                "type": "message_start",
                "event": {
                    "type": "message_start",
                    "message": {
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                    },
                },
            },
            {"kind": "event", "sequence": 1, "type": "future_bogus", "event": {"type": "future_bogus"}},
            {"kind": "event", "sequence": 2, "type": "message_stop", "event": {"type": "message_stop"}},
        ]
        self.assertFalse(probe._stream_order_ok("anthropic", events))
        turn = probe._parse_anthropic_stream(probe.TransportResult(200, None, events, False, 0, False))
        self.assertIn("stream_event_unknown", turn.parse_errors)

    def test_anthropic_stream_rejects_done_sentinel(self) -> None:
        events = [
            {"kind": "event", "sequence": 0, "type": "message_start", "event": {"type": "message_start"}},
            {"kind": "done", "sequence": 1, "type": None},
            {"kind": "event", "sequence": 2, "type": "message_stop", "event": {"type": "message_stop"}},
        ]
        self.assertFalse(probe._stream_order_ok("anthropic", events))

    def test_anthropic_stream_rejects_content_after_message_delta(self) -> None:
        events = [
            {"kind": "event", "sequence": 0, "type": "message_start", "event": {"type": "message_start"}},
            {"kind": "event", "sequence": 1, "type": "message_delta", "event": {"type": "message_delta", "delta": {"stop_reason": "tool_use"}}},
            {"kind": "event", "sequence": 2, "type": "content_block_start", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}},
            {"kind": "event", "sequence": 3, "type": "message_stop", "event": {"type": "message_stop"}},
        ]
        self.assertFalse(probe._stream_order_ok("anthropic", events))

    def test_responses_failure_event_invalidates_stream(self) -> None:
        events = [
            {"kind": "event", "sequence": 0, "type": "response.created", "event": {"type": "response.created", "response": {"object": "response", "status": "in_progress", "output": []}}},
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
            {"kind": "event", "type": "response.output_item.done", "event": {"type": "response.output_item.done", "item": {"id": "rs_1", "type": "reasoning", "summary": [{"type": "summary_text", "text": "summary"}]}}},
        ]
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, events, True, 0))
        self.assertTrue(turn.reasoning_present)
        self.assertEqual(turn.reasoning_text, "summary")

    def test_responses_stream_compares_reasoning_snapshot_to_delta(self) -> None:
        events = [
            {"kind": "event", "type": "response.output_item.added", "event": {"type": "response.output_item.added", "item": {"id": "rs_1", "type": "reasoning"}}},
            {"kind": "event", "type": "response.reasoning_summary_text.delta", "event": {"type": "response.reasoning_summary_text.delta", "item_id": "rs_1", "delta": "delta"}},
            {"kind": "event", "type": "response.output_item.done", "event": {"type": "response.output_item.done", "item": {"id": "rs_1", "type": "reasoning", "summary": [{"type": "summary_text", "text": "snapshot"}]} }},
        ]
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, events, False, 0))
        self.assertIn("stream_reasoning_snapshot_mismatch", turn.parse_errors)

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

    def test_responses_stream_in_progress_snapshot_requires_object(self) -> None:
        for response in (
            [],
            {"object": "other", "status": "in_progress", "output": []},
            {"object": "response", "status": "completed", "output": []},
            {"object": "response", "status": "in_progress", "output": [{"id": "msg_1"}]},
        ):
            events = _response_message_stream()
            events.insert(1, {"kind": "event", "type": "response.in_progress", "event": {"type": "response.in_progress", "response": response}})
            for index, event in enumerate(events):
                event["sequence"] = index
                event["wire_sequence"] = index
            self.assertFalse(probe._stream_order_ok("responses", events))
            turn = probe._parse_responses_stream(probe.TransportResult(200, None, events, False, 0, False))
            self.assertIn("response_in_progress_snapshot_invalid", turn.parse_errors)

    def test_responses_stream_in_progress_snapshot_preserves_response_id(self) -> None:
        valid = _response_message_stream()
        valid[0]["event"]["response"]["id"] = "resp_1"
        valid.insert(
            1,
            {
                "kind": "event",
                "type": "response.in_progress",
                "event": {
                    "type": "response.in_progress",
                    "response": {"id": "resp_1", "object": "response", "status": "in_progress", "output": []},
                },
            },
        )
        valid[-1]["event"]["response"]["id"] = "resp_1"
        for index, event in enumerate(valid):
            event["sequence"] = index
            event["wire_sequence"] = index
        self.assertTrue(probe._stream_order_ok("responses", valid))
        valid_turn = probe._parse_responses_stream(probe.TransportResult(200, None, valid, False, 0, False))
        self.assertNotIn("response_in_progress_id_invalid", valid_turn.parse_errors)

        missing = copy.deepcopy(valid)
        missing[1]["event"]["response"].pop("id")
        self.assertFalse(probe._stream_order_ok("responses", missing))
        missing_turn = probe._parse_responses_stream(probe.TransportResult(200, None, missing, False, 0, False))
        self.assertIn("response_in_progress_id_invalid", missing_turn.parse_errors)

    def test_responses_stream_rejects_invalid_terminal_discriminator(self) -> None:
        events = _response_message_stream()
        events[-1]["event"]["response"]["object"] = "other"
        self.assertFalse(probe._stream_order_ok("responses", events))
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, events, False, 0, False))
        self.assertIn("response_object_invalid", turn.parse_errors)

    def test_responses_stream_rejects_text_after_content_part_done(self) -> None:
        events = _response_message_stream()
        events[3], events[5] = events[5], events[3]
        for index, event in enumerate(events):
            event["sequence"] = index
            event["wire_sequence"] = index
        self.assertFalse(probe._stream_order_ok("responses", events))
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, events, False, 0, False))
        self.assertIn("stream_text_after_content_part_done", turn.parse_errors)

    def test_responses_stream_rejects_terminal_only_function_calls(self) -> None:
        events = [
            {"kind": "event", "type": "response.created", "event": {"type": "response.created", "response": {}}},
            {"kind": "event", "type": "response.completed", "event": {"type": "response.completed", "response": {"status": "completed", "output": [{"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "lookup_weather", "arguments": '{"city":"Shanghai"}' }]}}},
        ]
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, events, False, 0))
        self.assertIn("stream_arguments_missing", turn.parse_errors)

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
            {"kind": "event", "type": None, "event": {"object": "chat.completion.chunk", "choices": [{"delta": {"role": "user"}}]}},
            {"kind": "event", "type": None, "event": {"object": "chat.completion.chunk", "choices": [{"delta": {}, "finish_reason": "stop"}]}},
            {"kind": "done", "type": None},
        ]
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, invalid, True, 0))
        self.assertIn("assistant_role_invalid", turn.parse_errors)

    def test_chat_stream_rejects_malformed_delta(self) -> None:
        events = [
            {"kind": "event", "type": None, "event": {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": None}]}},
        ]
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, events, True, 0))
        self.assertIn("delta_invalid", turn.parse_errors)

    def test_chat_stream_rejects_choice_index_change(self) -> None:
        events = [
            {"kind": "event", "sequence": 0, "type": None, "event": {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "a"}}]}},
            {"kind": "event", "sequence": 1, "type": None, "event": {"object": "chat.completion.chunk", "choices": [{"index": 1, "delta": {"content": "b"}, "finish_reason": "stop"}]}},
            {"kind": "done", "sequence": 2, "type": None},
        ]
        self.assertFalse(probe._stream_order_ok("chat", events))
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, events, True, 0, False))
        self.assertIn("choice_index_invalid", turn.parse_errors)

    def test_chat_stream_order_rejects_content_after_finish(self) -> None:
        allowed = [
            {"kind": "event", "sequence": 0, "event": {"id": "chatcmpl_1", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}},
            {"kind": "event", "sequence": 1, "event": {"id": "chatcmpl_1", "object": "chat.completion.chunk", "choices": [], "usage": {"completion_tokens": 1}}},
            {"kind": "done", "sequence": 2},
        ]
        invalid = [
            allowed[0],
            {"kind": "event", "sequence": 1, "event": {"id": "chatcmpl_1", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": "late"}}]}},
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
                    {"type": "thinking", "thinking": "PRIVATE_REASONING", "signature": "sig"},
                    {"type": "tool_use", "id": "call_1", "name": "lookup_weather", "input": {"city": "Shanghai"}},
                    {"type": "text", "text": "PRIVATE_REASONING"},
                ],
                "stop_reason": "tool_use",
            }
        )
        projection = probe._validate_first(turn, ("lookup_weather",), stream=False)
        self.assertFalse(projection["checks"]["reasoning_not_visible"])

    def test_opaque_reasoning_payloads_must_not_be_visible(self) -> None:
        signature = probe._parse_anthropic_document(
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "PRIVATE_THOUGHT", "signature": "PRIVATE_SIGNATURE"},
                    {"type": "tool_use", "id": "call_1", "name": "lookup_weather", "input": {"city": "Shanghai"}},
                    {"type": "text", "text": "PRIVATE_SIGNATURE"},
                ],
                "stop_reason": "tool_use",
            }
        )
        self.assertEqual(signature.reasoning_text, "PRIVATE_THOUGHT")
        self.assertFalse(
            probe._validate_first(signature, ("lookup_weather",), stream=False)["checks"]["reasoning_not_visible"]
        )

        redacted = probe._parse_anthropic_document(
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "redacted_thinking", "data": "PRIVATE_REDACTED"},
                    {"type": "tool_use", "id": "call_1", "name": "lookup_weather", "input": {"city": "Shanghai"}},
                    {"type": "text", "text": "PRIVATE_REDACTED"},
                ],
                "stop_reason": "tool_use",
            }
        )
        self.assertFalse(
            probe._validate_first(redacted, ("lookup_weather",), stream=False)["checks"]["reasoning_not_visible"]
        )

        encrypted = probe._parse_responses_document(
            {
                "object": "response",
                "output": [
                    {"id": "rs_1", "type": "reasoning", "encrypted_content": "PRIVATE_ENCRYPTED"},
                    {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "PRIVATE_ENCRYPTED"}],
                    },
                    {
                        "id": "fc_1",
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "lookup_weather",
                        "arguments": '{"city":"Shanghai"}',
                    },
                ],
                "status": "completed",
            }
        )
        self.assertEqual(encrypted.reasoning_text, "")
        self.assertFalse(
            probe._validate_first(encrypted, ("lookup_weather",), stream=False)["checks"]["reasoning_not_visible"]
        )

        second = probe._parse_anthropic_document(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "PRIVATE_SIGNATURE"}],
                "stop_reason": "end_turn",
            }
        )
        self.assertFalse(
            probe._validate_second(
                second,
                (),
                stream=False,
                prior_reasoning_parts=signature.reasoning_parts,
            )["checks"]["reasoning_not_visible"]
        )

    def test_first_turn_rejects_premature_tool_output_evidence(self) -> None:
        for text in (
            "tool=lookup_weather;call_id=call_1;marker=WEATHER_OK",
            "WEATHER_OK",
        ):
            turn = probe._parse_anthropic_document(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "PRIVATE_THOUGHT", "signature": "sig"},
                        {"type": "tool_use", "id": "call_1", "name": "lookup_weather", "input": {"city": "Shanghai"}},
                        {"type": "text", "text": text},
                    ],
                    "stop_reason": "tool_use",
                }
            )
            projection = probe._validate_first(turn, ("lookup_weather",), stream=False)
            self.assertFalse(projection["checks"]["no_premature_tool_outputs"])

    def test_final_reasoning_text_must_not_be_visible(self) -> None:
        turn = probe._parse_responses_document(
            {
                "object": "response",
                "output": [
                    {"type": "reasoning", "summary": [{"type": "summary_text", "text": "PRIVATE_REASONING"}]},
                    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "PRIVATE_REASONING"}]},
                ],
                "status": "completed",
            }
        )
        projection = probe._validate_second(turn, (), stream=False)
        self.assertFalse(projection["checks"]["reasoning_not_visible"])

    def test_final_turn_requires_reasoning_signal(self) -> None:
        turn = probe._parse_anthropic_document(
            {"content": [{"type": "text", "text": f"{probe.SYSTEM_MARKER} {probe.SYSTEM_SCOPE_OK} WEATHER_OK"}], "stop_reason": "end_turn"}
        )
        projection = probe._validate_second(turn, (), stream=False)
        self.assertFalse(projection["checks"]["reasoning_present"])

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

    def test_responses_text_done_must_match_reconstructed_text(self) -> None:
        for event_type, item_type in (
            ("response.output_text", "message"),
            ("response.reasoning_summary_text", "reasoning"),
        ):
            events = [
                {"kind": "event", "event": {"type": "response.output_item.added", "item": {"id": "item_1", "type": item_type, "role": "assistant"}}},
                {"kind": "event", "event": {"type": f"{event_type}.delta", "item_id": "item_1", "content_index": 0, "delta": "delta"}},
                {"kind": "event", "event": {"type": f"{event_type}.done", "item_id": "item_1", "content_index": 0, "text": "different"}},
            ]
            turn = probe._parse_responses_stream(probe.TransportResult(200, None, events, False, 0))
            self.assertIn("stream_text_done_mismatch", turn.parse_errors)

    def test_streamed_chat_content_requires_string_or_null(self) -> None:
        events = [
            {"kind": "event", "event": {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant", "content": {"text": probe.SYSTEM_MARKER}}}]}},
            {"kind": "event", "event": {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}},
            {"kind": "done"},
        ]
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, events, True, 0, True))
        self.assertIn("stream_content_invalid", turn.parse_errors)

    def test_anthropic_text_blocks_require_strings(self) -> None:
        turn = probe._parse_anthropic_document(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": {"marker": probe.SYSTEM_MARKER}}],
                "stop_reason": "end_turn",
            }
        )
        self.assertIn("text_block_invalid", turn.parse_errors)

    def test_responses_done_message_content_requires_list(self) -> None:
        events = [
            {"kind": "event", "event": {"type": "response.output_item.added", "item": {"id": "msg_1", "type": "message", "role": "assistant"}}},
            {"kind": "event", "event": {"type": "response.output_item.done", "item": {"id": "msg_1", "type": "message", "role": "assistant", "content": None}}},
        ]
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, events, False, 0))
        self.assertIn("message_content_invalid", turn.parse_errors)

    def test_chat_tool_calls_require_list(self) -> None:
        turn = probe._parse_chat_document(
            {"choices": [{"index": 0, "message": {"role": "assistant", "content": "", "tool_calls": None}, "finish_reason": "stop"}]}
        )
        self.assertIn("tool_calls_invalid", turn.parse_errors)

    def test_anthropic_message_delta_requires_object(self) -> None:
        events = [
            {"kind": "event", "sequence": 0, "type": "message_start", "event": {"type": "message_start"}},
            {"kind": "event", "sequence": 1, "type": "message_delta", "event": {"type": "message_delta", "delta": None}},
            {"kind": "event", "sequence": 2, "type": "message_stop", "event": {"type": "message_stop"}},
        ]
        self.assertFalse(probe._stream_order_ok("anthropic", events))
        turn = probe._parse_anthropic_stream(probe.TransportResult(200, None, events, False, 0, False))
        self.assertIn("message_delta_invalid", turn.parse_errors)

    def test_responses_reject_done_sentinel(self) -> None:
        events = [
            {"kind": "event", "sequence": 0, "type": "response.created", "event": {"type": "response.created", "response": {}}},
            {"kind": "done", "sequence": 1},
            {"kind": "event", "sequence": 2, "type": "response.completed", "event": {"type": "response.completed", "response": {"status": "completed", "output": []}}},
        ]
        self.assertFalse(probe._stream_order_ok("responses", events))

    def test_responses_output_index_must_remain_consistent(self) -> None:
        events = [
            {"kind": "event", "sequence": 0, "type": "response.created", "event": {"type": "response.created", "response": {}}},
            {"kind": "event", "sequence": 1, "type": "response.output_item.added", "event": {"type": "response.output_item.added", "output_index": 0, "item": {"id": "msg_1", "type": "message"}}},
            {"kind": "event", "sequence": 2, "type": "response.output_text.delta", "event": {"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 1, "delta": "text"}},
        ]
        self.assertFalse(probe._stream_order_ok("responses", events))

    def test_anthropic_tool_start_requires_empty_input_snapshot(self) -> None:
        events = [
            {"kind": "event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "call_1", "name": "lookup_weather", "input": {"city": "Paris"}}}},
            {"kind": "event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"city":"Shanghai"}'}}},
        ]
        turn = probe._parse_anthropic_stream(probe.TransportResult(200, None, events, False, 0))
        self.assertIn("stream_tool_input_snapshot_invalid", turn.parse_errors)

    def test_final_markers_require_exact_tokens(self) -> None:
        call = probe.ToolCall("call_1", "lookup_weather", {"city": "Shanghai"})
        turn = probe._parse_anthropic_document(
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"{probe.SYSTEM_MARKER}_BAD {probe.SYSTEM_SCOPE_OK}_BAD "
                            "tool=lookup_weather;call_id=call_1;marker=WEATHER_OK_CORRUPTED"
                        ),
                    }
                ],
                "stop_reason": "end_turn",
            }
        )
        checks = probe._validate_second(turn, ("lookup_weather",), stream=False, expected_calls=[call])["checks"]
        self.assertFalse(checks["system_marker"])
        self.assertFalse(checks["system_scope"])
        self.assertFalse(checks["tool_outputs"])
        self.assertFalse(checks["tool_output_call_pairs"])

    def test_protocol_argument_wire_encodings_are_enforced(self) -> None:
        anthropic = probe._parse_anthropic_document(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "call_1", "name": "lookup_weather", "input": '{"city":"Shanghai"}'}],
                "stop_reason": "tool_use",
            }
        )
        responses = probe._parse_responses_document(
            {"output": [{"type": "function_call", "call_id": "call_1", "name": "lookup_weather", "arguments": {"city": "Shanghai"}}], "status": "completed"}
        )
        chat = probe._parse_chat_document(
            {"choices": [{"index": 0, "message": {"role": "assistant", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "lookup_weather", "arguments": {"city": "Shanghai"}}}]}, "finish_reason": "tool_calls"}]}
        )
        self.assertIn("arguments_not_object", anthropic.parse_errors)
        self.assertIn("arguments_not_json_string", responses.parse_errors)
        self.assertIn("arguments_not_json_string", chat.parse_errors)

    def test_transient_encrypted_reasoning_is_not_retained(self) -> None:
        events = [
            {"kind": "event", "event": {"type": "response.output_item.added", "item": {"id": "rs_1", "type": "reasoning", "encrypted_content": "opaque"}}},
            {"kind": "event", "event": {"type": "response.output_item.done", "item": {"id": "rs_1", "type": "reasoning", "summary": []}}},
        ]
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, events, False, 0))
        self.assertFalse(turn.reasoning_present)

    def test_chat_usage_before_finish_is_rejected(self) -> None:
        events = [
            {"kind": "event", "sequence": 0, "event": {"object": "chat.completion.chunk", "choices": [], "usage": {"completion_tokens_details": {"reasoning_tokens": 1}}}},
            {"kind": "event", "sequence": 1, "event": {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": "tool_calls"}]}},
            {"kind": "done", "sequence": 2},
        ]
        self.assertFalse(probe._stream_order_ok("chat", events))
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, events, True, 0, False))
        self.assertIn("usage_before_finish", turn.parse_errors)
        self.assertFalse(turn.reasoning_present)

    def test_responses_done_function_snapshot_requires_arguments(self) -> None:
        for arguments in (None, {"city": "Shanghai"}):
            events = [
                {"kind": "event", "event": {"type": "response.output_item.added", "item": {"id": "fc_1", "type": "function_call"}}},
                {"kind": "event", "event": {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": '{"city":"Shanghai"}'}},
                {"kind": "event", "event": {"type": "response.output_item.done", "item": {"id": "fc_1", "type": "function_call", "arguments": arguments}}},
            ]
            turn = probe._parse_responses_stream(probe.TransportResult(200, None, events, False, 0))
            self.assertIn("stream_arguments_snapshot_invalid", turn.parse_errors)

    def test_responses_function_call_requires_arguments_done_event(self) -> None:
        events = [
            {"kind": "event", "event": {"type": "response.output_item.added", "item": {"id": "fc_1", "type": "function_call"}}},
            {"kind": "event", "event": {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": '{"city":"Shanghai"}'}},
            {"kind": "event", "event": {"type": "response.output_item.done", "item": {"id": "fc_1", "type": "function_call", "arguments": '{"city":"Shanghai"}'}}},
        ]
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, events, False, 0))
        self.assertIn("stream_arguments_done_missing", turn.parse_errors)

    def test_responses_completed_requires_literal_status(self) -> None:
        for status in (None, "incomplete"):
            events = [
                {"kind": "event", "event": {"type": "response.completed", "response": {"status": status, "output": []}}},
            ]
            turn = probe._parse_responses_stream(probe.TransportResult(200, None, events, False, 0))
            self.assertIn("terminal_status_invalid", turn.parse_errors)
            self.assertFalse(turn.terminal)

    def test_responses_wire_sequence_must_increase(self) -> None:
        events = _responses_wire(
            {"type": "response.created", "response": {}},
            {"type": "response.completed", "response": {"status": "completed", "output": []}},
        )
        events[1]["wire_sequence"] = 2
        self.assertTrue(probe._stream_order_ok("responses", events))
        duplicate = copy.deepcopy(events)
        duplicate[1]["wire_sequence"] = 0
        self.assertFalse(probe._stream_order_ok("responses", duplicate))

    def test_responses_content_part_done_must_match_text(self) -> None:
        events = [
            {"kind": "event", "event": {"type": "response.output_item.added", "item": {"id": "msg_1", "type": "message", "role": "assistant"}}},
            {"kind": "event", "event": {"type": "response.content_part.added", "item_id": "msg_1", "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": ""}}},
            {"kind": "event", "event": {"type": "response.output_text.delta", "item_id": "msg_1", "delta": "answer"}},
            {"kind": "event", "event": {"type": "response.content_part.done", "item_id": "msg_1", "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": "different"}}},
        ]
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, events, False, 0))
        self.assertIn("content_part_snapshot_mismatch", turn.parse_errors)

    def test_anthropic_block_indexes_must_be_contiguous(self) -> None:
        events = [
            {"kind": "event", "sequence": 0, "type": "message_start", "event": {"type": "message_start"}},
            {"kind": "event", "sequence": 1, "type": "content_block_start", "event": {"type": "content_block_start", "index": 5, "content_block": {"type": "text"}}},
            {"kind": "event", "sequence": 2, "type": "message_stop", "event": {"type": "message_stop"}},
        ]
        self.assertFalse(probe._stream_order_ok("anthropic", events))

    def test_chat_tool_indexes_must_be_contiguous(self) -> None:
        events = [
            {"kind": "event", "sequence": 0, "event": {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [{"index": 5, "id": "call_1", "type": "function", "function": {"name": "lookup_weather", "arguments": "{"}}]}}]}},
            {"kind": "event", "sequence": 1, "event": {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 7, "id": "call_2", "type": "function", "function": {"name": "lookup_time", "arguments": "}"}}]}, "finish_reason": "tool_calls"}]}},
            {"kind": "done", "sequence": 2},
        ]
        self.assertFalse(probe._stream_order_ok("chat", events))

    def test_responses_duplicate_created_event_is_rejected(self) -> None:
        events = [
            {"kind": "event", "sequence": 0, "wire_sequence": 0, "type": "response.created", "event": {"type": "response.created", "response": {"id": "resp_1"}}},
            {"kind": "event", "sequence": 1, "wire_sequence": 1, "type": "response.created", "event": {"type": "response.created", "response": {"id": "resp_2"}}},
        ]
        self.assertFalse(probe._stream_order_ok("responses", events))

    def test_responses_text_events_require_matching_content_indexes(self) -> None:
        valid = _response_message_stream()
        sparse = _response_message_stream()
        for item in sparse:
            if "content_index" in item["event"]:
                item["event"]["content_index"] = 5
        mismatched = _response_message_stream()
        mismatched[3]["event"]["content_index"] = 1
        self.assertTrue(probe._stream_order_ok("responses", valid))
        self.assertFalse(probe._stream_order_ok("responses", sparse))
        self.assertFalse(probe._stream_order_ok("responses", mismatched))

    def test_responses_text_done_is_required_before_item_completion(self) -> None:
        valid_message = _response_message_stream()
        valid_reasoning = _response_reasoning_stream()
        message_events = _responses_wire(*(item["event"] for item in valid_message if item["type"] != "response.output_text.done"))
        reasoning_events = _responses_wire(*(item["event"] for item in valid_reasoning if item["type"] != "response.reasoning_summary_text.done"))
        self.assertTrue(probe._stream_order_ok("responses", valid_message))
        self.assertTrue(probe._stream_order_ok("responses", valid_reasoning))
        self.assertFalse(probe._stream_order_ok("responses", message_events))
        self.assertFalse(probe._stream_order_ok("responses", reasoning_events))

    def test_responses_stream_item_ids_must_be_nonempty_strings(self) -> None:
        invalid_added = _response_message_stream()
        invalid_added[1]["event"]["item"]["id"] = []
        invalid_done = _response_message_stream()
        invalid_done[6]["event"]["item"]["id"] = []
        self.assertFalse(probe._stream_order_ok("responses", invalid_added))
        self.assertFalse(probe._stream_order_ok("responses", invalid_done))
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, invalid_added, False, 0, False))
        self.assertIn("stream_item_id_invalid", turn.parse_errors)

    def test_responses_nonstream_status_must_be_a_string(self) -> None:
        for status in ([], {}):
            turn = probe._parse_responses_document({"output": [], "status": status})
            self.assertIn("status_invalid", turn.parse_errors)
            self.assertFalse(turn.terminal)

    def test_anthropic_stream_start_snapshots_require_strings(self) -> None:
        text_events = [
            {"kind": "event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": []}}},
            {"kind": "event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "answer"}}},
        ]
        thinking_events = [
            {"kind": "event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": {}, "signature": []}}},
            {"kind": "event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "thought"}}},
            {"kind": "event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}}},
        ]
        text_turn = probe._parse_anthropic_stream(probe.TransportResult(200, None, text_events, False, 0))
        thinking_turn = probe._parse_anthropic_stream(probe.TransportResult(200, None, thinking_events, False, 0))
        self.assertIn("stream_text_snapshot_invalid", text_turn.parse_errors)
        self.assertIn("stream_thinking_snapshot_invalid", thinking_turn.parse_errors)
        self.assertIn("stream_signature_snapshot_invalid", thinking_turn.parse_errors)

        prefilled = copy.deepcopy(text_events)
        prefilled[0]["event"]["content_block"]["text"] = "answer"
        self.assertFalse(probe._stream_order_ok("anthropic", prefilled))
        prefilled_turn = probe._parse_anthropic_stream(probe.TransportResult(200, None, prefilled, False, 0))
        self.assertIn("stream_text_opening_snapshot_invalid", prefilled_turn.parse_errors)

    def test_responses_function_opening_arguments_must_be_empty(self) -> None:
        valid = _response_function_stream()
        invalid = copy.deepcopy(valid)
        invalid[1]["event"]["item"]["arguments"] = '{"city":"Paris"}'
        self.assertTrue(probe._stream_order_ok("responses", valid))
        self.assertFalse(probe._stream_order_ok("responses", invalid))
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, invalid, False, 0, False))
        self.assertIn("stream_arguments_opening_snapshot_invalid", turn.parse_errors)

    def test_responses_output_item_opening_status_must_be_in_progress(self) -> None:
        invalid = _response_function_stream()
        invalid[1]["event"]["item"]["status"] = "failed"
        self.assertFalse(probe._stream_order_ok("responses", invalid))
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, invalid, False, 0, False))
        self.assertIn("stream_output_item_status_invalid", turn.parse_errors)

        valid = _response_function_stream()
        valid[1]["event"]["item"]["status"] = "in_progress"
        valid[4]["event"]["item"]["status"] = "completed"
        valid[-1]["event"]["response"]["output"][0]["status"] = "completed"
        self.assertTrue(probe._stream_order_ok("responses", valid))
        valid_turn = probe._parse_responses_stream(probe.TransportResult(200, None, valid, False, 0, False))
        self.assertNotIn("stream_output_item_status_invalid", valid_turn.parse_errors)

        missing_done_status = copy.deepcopy(valid)
        missing_done_status[4]["event"]["item"].pop("status")
        self.assertFalse(probe._stream_order_ok("responses", missing_done_status))
        missing_turn = probe._parse_responses_stream(probe.TransportResult(200, None, missing_done_status, False, 0, False))
        self.assertIn("stream_output_item_status_invalid", missing_turn.parse_errors)

    def test_responses_message_opening_content_must_be_absent_or_empty(self) -> None:
        valid = _response_message_stream()
        self.assertTrue(probe._stream_order_ok("responses", valid))
        for opening_content in (None, [{"type": "output_text", "text": "prefilled"}], {"unexpected": True}):
            invalid = copy.deepcopy(valid)
            invalid[1]["event"]["item"]["content"] = opening_content
            self.assertFalse(probe._stream_order_ok("responses", invalid))
            turn = probe._parse_responses_stream(probe.TransportResult(200, None, invalid, False, 0, False))
            self.assertIn("stream_message_opening_snapshot_invalid", turn.parse_errors)

        empty = copy.deepcopy(valid)
        empty[1]["event"]["item"]["content"] = []
        self.assertTrue(probe._stream_order_ok("responses", empty))

    def test_responses_stream_rejects_noncompleted_terminal_item_status(self) -> None:
        valid = _response_function_stream()
        valid[4]["event"]["item"]["status"] = "completed"
        valid[-1]["event"]["response"]["output"][0]["status"] = "completed"
        self.assertTrue(probe._stream_order_ok("responses", valid))
        valid_turn = probe._parse_responses_stream(probe.TransportResult(200, None, valid, False, 0, False))
        self.assertNotIn("terminal_output_item_status_invalid", valid_turn.parse_errors)

        invalid = copy.deepcopy(valid)
        invalid[-1]["event"]["response"]["output"][0]["status"] = "failed"
        self.assertFalse(probe._stream_order_ok("responses", invalid))
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, invalid, False, 0, False))
        self.assertIn("terminal_output_item_status_invalid", turn.parse_errors)
        self.assertIn("terminal_output_mismatch", turn.parse_errors)

    def test_responses_stream_requires_terminal_response_id(self) -> None:
        valid = _response_message_stream()
        valid[0]["event"]["response"]["id"] = "resp_1"
        valid[-1]["event"]["response"]["id"] = "resp_1"
        self.assertTrue(probe._stream_order_ok("responses", valid))
        valid_turn = probe._parse_responses_stream(probe.TransportResult(200, None, valid, False, 0, False))
        self.assertNotIn("terminal_response_id_invalid", valid_turn.parse_errors)

        missing = copy.deepcopy(valid)
        missing[-1]["event"]["response"].pop("id")
        self.assertFalse(probe._stream_order_ok("responses", missing))
        missing_turn = probe._parse_responses_stream(probe.TransportResult(200, None, missing, False, 0, False))
        self.assertIn("terminal_response_id_invalid", missing_turn.parse_errors)

        mismatched = copy.deepcopy(valid)
        mismatched[-1]["event"]["response"]["id"] = "resp_2"
        self.assertFalse(probe._stream_order_ok("responses", mismatched))
        mismatched_turn = probe._parse_responses_stream(probe.TransportResult(200, None, mismatched, False, 0, False))
        self.assertIn("terminal_response_id_invalid", mismatched_turn.parse_errors)

    def test_responses_stream_requires_created_response_id(self) -> None:
        valid = _response_message_stream()
        self.assertTrue(probe._stream_order_ok("responses", valid))

        for invalid_id in (None, ""):
            invalid = copy.deepcopy(valid)
            if invalid_id is None:
                invalid[0]["event"]["response"].pop("id")
            else:
                invalid[0]["event"]["response"]["id"] = invalid_id
            self.assertFalse(probe._stream_order_ok("responses", invalid))
            turn = probe._parse_responses_stream(probe.TransportResult(200, None, invalid, False, 0, False))
            self.assertIn("response_id_invalid", turn.parse_errors)

    def test_responses_function_opening_snapshot_requires_identity(self) -> None:
        for field in ("call_id", "name"):
            invalid = _response_function_stream()
            invalid[1]["event"]["item"].pop(field)
            self.assertFalse(probe._stream_order_ok("responses", invalid))
            turn = probe._parse_responses_stream(probe.TransportResult(200, None, invalid, False, 0, False))
            self.assertIn("stream_function_identity_invalid", turn.parse_errors)

    def test_responses_content_part_opening_text_must_be_empty(self) -> None:
        valid = _response_message_stream()
        invalid = copy.deepcopy(valid)
        invalid[2]["event"]["part"]["text"] = "WRONG"
        self.assertTrue(probe._stream_order_ok("responses", valid))
        self.assertFalse(probe._stream_order_ok("responses", invalid))
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, invalid, False, 0, False))
        self.assertIn("content_part_opening_snapshot_invalid", turn.parse_errors)

    def test_responses_stream_preserves_multiple_content_parts(self) -> None:
        events = _response_multi_content_stream()
        self.assertTrue(probe._stream_order_ok("responses", events))
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, events, False, 0, True))
        self.assertNotIn("terminal_output_mismatch", turn.parse_errors)
        self.assertEqual(turn.text, "firstsecond")
        self.assertEqual(turn.continuation[0]["content"], [{"type": "output_text", "text": "first"}, {"type": "output_text", "text": "second"}])

    def test_responses_text_content_part_accepts_output_text_deltas(self) -> None:
        events = _response_message_stream()
        events[2]["event"]["part"]["type"] = "text"
        events[5]["event"]["part"]["type"] = "text"
        events[6]["event"]["item"]["content"][0]["type"] = "text"
        events[7]["event"]["response"]["output"][0]["content"][0]["type"] = "text"
        self.assertTrue(probe._stream_order_ok("responses", events))
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, events, False, 0, True))
        self.assertEqual(turn.text, "answer")
        self.assertEqual(turn.parse_errors, [])

    def test_responses_text_content_part_opening_text_must_be_empty(self) -> None:
        events = [
            {"kind": "event", "type": "response.created", "event": {"type": "response.created", "response": {}}},
            {"kind": "event", "type": "response.output_item.added", "event": {"type": "response.output_item.added", "output_index": 0, "item": {"id": "msg_1", "type": "message", "role": "assistant"}}},
            {"kind": "event", "type": "response.content_part.added", "event": {"type": "response.content_part.added", "item_id": "msg_1", "output_index": 0, "content_index": 0, "part": {"type": "text", "text": "prefilled"}}},
        ]
        self.assertFalse(probe._stream_order_ok("responses", events))
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, events, False, 0, False))
        self.assertIn("content_part_opening_snapshot_invalid", turn.parse_errors)

    def test_responses_stream_rejects_unsupported_content_part_type(self) -> None:
        for part_type in ("future_part", []):
            invalid = _response_message_stream()
            invalid[2]["event"]["part"]["type"] = part_type
            turn = probe._parse_responses_stream(probe.TransportResult(200, None, invalid, False, 0, False))
            self.assertFalse(probe._stream_order_ok("responses", invalid))
            self.assertIn("content_part_type_invalid", turn.parse_errors)

    def test_responses_stream_content_parts_match_item_snapshot(self) -> None:
        invalid = _response_message_stream()
        invalid[-1]["event"]["response"]["output"][0]["content"][0]["type"] = "text"
        self.assertFalse(probe._stream_order_ok("responses", invalid))
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, invalid, False, 0, False))
        self.assertIn("terminal_output_mismatch", turn.parse_errors)

    def test_responses_stream_rejects_unhashable_message_part_type(self) -> None:
        invalid = _response_message_stream()
        invalid[6]["event"]["item"]["content"][0]["type"] = []
        self.assertFalse(probe._stream_order_ok("responses", invalid))
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, invalid, False, 0, False))
        self.assertIn("message_part_type_invalid", turn.parse_errors)

    def test_anthropic_thinking_opening_snapshot_must_be_empty(self) -> None:
        valid = [
            {"kind": "event", "sequence": 0, "type": "message_start", "event": {"type": "message_start", "message": {"type": "message", "role": "assistant", "content": [], "stop_reason": None, "stop_sequence": None}}},
            {"kind": "event", "sequence": 1, "type": "content_block_start", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}}},
            {"kind": "event", "sequence": 2, "type": "content_block_delta", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "thought"}}},
            {"kind": "event", "sequence": 3, "type": "content_block_delta", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}}},
            {"kind": "event", "sequence": 4, "type": "content_block_stop", "event": {"type": "content_block_stop", "index": 0}},
            {"kind": "event", "sequence": 5, "type": "message_delta", "event": {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}}},
            {"kind": "event", "sequence": 6, "type": "message_stop", "event": {"type": "message_stop"}},
        ]
        invalid = copy.deepcopy(valid)
        invalid[1]["event"]["content_block"]["thinking"] = "prefilled"
        invalid[1]["event"]["content_block"]["signature"] = "sig"
        self.assertTrue(probe._stream_order_ok("anthropic", valid))
        self.assertFalse(probe._stream_order_ok("anthropic", invalid))
        turn = probe._parse_anthropic_stream(probe.TransportResult(200, None, invalid, False, 0, False))
        self.assertIn("stream_thinking_opening_snapshot_invalid", turn.parse_errors)
        self.assertIn("stream_signature_opening_snapshot_invalid", turn.parse_errors)

    def test_anthropic_thinking_rejects_deltas_after_signature(self) -> None:
        events = [
            {"kind": "event", "sequence": 0, "type": "message_start", "event": {"type": "message_start", "message": {"type": "message", "role": "assistant", "content": [], "stop_reason": None, "stop_sequence": None}}},
            {"kind": "event", "sequence": 1, "type": "content_block_start", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}}},
            {"kind": "event", "sequence": 2, "type": "content_block_delta", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "thought"}}},
            {"kind": "event", "sequence": 3, "type": "content_block_delta", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}}},
            {"kind": "event", "sequence": 4, "type": "content_block_delta", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "late"}}},
            {"kind": "event", "sequence": 5, "type": "content_block_stop", "event": {"type": "content_block_stop", "index": 0}},
            {"kind": "event", "sequence": 6, "type": "message_delta", "event": {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}}},
            {"kind": "event", "sequence": 7, "type": "message_stop", "event": {"type": "message_stop"}},
        ]
        self.assertFalse(probe._stream_order_ok("anthropic", events))
        turn = probe._parse_anthropic_stream(probe.TransportResult(200, None, events, False, 0, False))
        self.assertIn("stream_delta_after_signature", turn.parse_errors)

    def test_anthropic_message_start_requires_initial_snapshot(self) -> None:
        valid = [
            {
                "kind": "event",
                "sequence": 0,
                "type": "message_start",
                "event": {
                    "type": "message_start",
                    "message": {
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                    },
                },
            },
            {
                "kind": "event",
                "sequence": 1,
                "type": "message_delta",
                "event": {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}},
            },
            {"kind": "event", "sequence": 2, "type": "message_stop", "event": {"type": "message_stop"}},
        ]
        nonempty_content = copy.deepcopy(valid)
        nonempty_content[0]["event"]["message"]["content"] = [{"type": "text", "text": "WRONG"}]
        premature_stop = copy.deepcopy(valid)
        premature_stop[0]["event"]["message"]["stop_reason"] = "end_turn"
        self.assertTrue(probe._stream_order_ok("anthropic", valid))
        self.assertFalse(probe._stream_order_ok("anthropic", nonempty_content))
        self.assertFalse(probe._stream_order_ok("anthropic", premature_stop))
        content_turn = probe._parse_anthropic_stream(
            probe.TransportResult(200, None, nonempty_content, False, 0, False)
        )
        stop_turn = probe._parse_anthropic_stream(probe.TransportResult(200, None, premature_stop, False, 0, False))
        self.assertIn("message_start_content_invalid", content_turn.parse_errors)
        self.assertIn("message_start_terminal_invalid", stop_turn.parse_errors)

        for field in ("stop_reason", "stop_sequence"):
            missing = copy.deepcopy(valid)
            missing[0]["event"]["message"].pop(field)
            self.assertFalse(probe._stream_order_ok("anthropic", missing))
            missing_turn = probe._parse_anthropic_stream(
                probe.TransportResult(200, None, missing, False, 0, False)
            )
            self.assertIn("message_start_terminal_invalid", missing_turn.parse_errors)

    def test_non_string_stop_reasons_are_parse_failures(self) -> None:
        anthropic = probe._parse_anthropic_document(
            {"type": "message", "role": "assistant", "content": [], "stop_reason": []}
        )
        chat = probe._parse_chat_document(
            {
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": {}}
                ]
            }
        )
        self.assertIn("stop_reason_invalid", anthropic.parse_errors)
        self.assertIn("finish_reason_invalid", chat.parse_errors)
        self.assertFalse(probe._validate_first(anthropic, (), stream=False)["checks"]["stop_reason"])
        self.assertFalse(probe._validate_first(chat, (), stream=False)["checks"]["stop_reason"])

    def test_chat_stream_choices_must_be_a_list(self) -> None:
        for choices in ({}, "invalid"):
            events = [{"kind": "event", "sequence": 0, "event": {"object": "chat.completion.chunk", "choices": choices}}]
            self.assertFalse(probe._stream_order_ok("chat", events))
            turn = probe._parse_chat_stream(probe.TransportResult(200, None, events, False, 0, False))
            self.assertIn("choice_invalid", turn.parse_errors)

    def test_responses_nonstream_requires_response_object(self) -> None:
        valid = probe._parse_responses_document({"object": "response", "output": [], "status": "completed"})
        self.assertNotIn("response_object_invalid", valid.parse_errors)
        for object_value in (None, "chat.completion", []):
            document = {"output": [], "status": "completed"}
            if object_value is not None:
                document["object"] = object_value
            turn = probe._parse_responses_document(document)
            self.assertIn("response_object_invalid", turn.parse_errors)

    def test_responses_nonstream_rejects_invalid_output_item_types(self) -> None:
        for item_type in (None, [], {}, "unknown"):
            item = {"id": "bad_1", "type": item_type}
            turn = probe._parse_responses_document({"object": "response", "output": [item], "status": "completed"})
            self.assertIn("output_item_type_invalid", turn.parse_errors)

    def test_responses_nonstream_rejects_duplicate_output_item_ids(self) -> None:
        output = [
            {"id": "same", "type": "reasoning", "summary": []},
            {"id": "same", "type": "function_call", "call_id": "call_1", "name": "lookup_weather", "arguments": '{"city":"Shanghai"}'},
        ]
        turn = probe._parse_responses_document({"object": "response", "output": output, "status": "completed"})
        self.assertIn("output_item_id_duplicate", turn.parse_errors)

    def test_responses_nonstream_rejects_invalid_nested_part_types(self) -> None:
        message = {"id": "msg_1", "type": "message", "role": "assistant", "content": [{"type": []}]}
        reasoning = {"id": "rs_1", "type": "reasoning", "summary": [{"type": {}}]}
        turn = probe._parse_responses_document({"object": "response", "output": [message, reasoning], "status": "completed"})
        self.assertIn("message_part_type_invalid", turn.parse_errors)
        self.assertIn("reasoning_part_type_invalid", turn.parse_errors)

    def test_responses_nonstream_rejects_unknown_message_part_types(self) -> None:
        message = {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "ok"}, {"type": "future_part", "text": "ignored"}],
        }
        turn = probe._parse_responses_document({"object": "response", "output": [message], "status": "completed"})
        self.assertIn("message_part_type_invalid", turn.parse_errors)

    def test_responses_nonstream_rejects_invalid_encrypted_reasoning(self) -> None:
        turn = probe._parse_responses_document(
            {
                "object": "response",
                "output": [{"type": "reasoning", "summary": [{"type": "summary_text", "text": "thought"}], "encrypted_content": []}],
                "status": "completed",
            }
        )
        self.assertIn("encrypted_reasoning_invalid", turn.parse_errors)

    def test_responses_nonstream_rejects_incomplete_function_call_items(self) -> None:
        item = {
            "id": "fc_1",
            "type": "function_call",
            "status": "incomplete",
            "call_id": "call_1",
            "name": "lookup_weather",
            "arguments": '{"city":"Shanghai"}',
        }
        turn = probe._parse_responses_document({"object": "response", "output": [item], "status": "completed"})
        self.assertIn("output_item_status_invalid", turn.parse_errors)
        self.assertEqual(turn.tool_calls, [])

    def test_responses_stream_event_type_must_be_a_string(self) -> None:
        for event_type in ([], {}):
            events = [
                {
                    "kind": "event",
                    "sequence": 0,
                    "wire_sequence": 0,
                    "type": event_type,
                    "event": {"type": event_type},
                }
            ]
            self.assertFalse(probe._stream_order_ok("responses", events))
            turn = probe._parse_responses_stream(probe.TransportResult(200, None, events, False, 0, False))
            self.assertIn("stream_event_type_invalid", turn.parse_errors)

    def test_responses_stream_rejects_unknown_event_types(self) -> None:
        events = _response_message_stream()
        events.insert(-1, {"kind": "event", "type": "response.future_bogus", "event": {"type": "response.future_bogus"}})
        for index, item in enumerate(events):
            item["sequence"] = index
            item["wire_sequence"] = index
        self.assertFalse(probe._stream_order_ok("responses", events))
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, events, False, 0, False))
        self.assertIn("stream_event_unknown", turn.parse_errors)

    def test_responses_reasoning_encrypted_snapshots_must_match(self) -> None:
        valid = _response_reasoning_stream()
        valid[1]["event"]["item"]["encrypted_content"] = "opaque"
        valid[4]["event"]["item"]["encrypted_content"] = "opaque"
        valid[5]["event"]["response"]["output"][0]["encrypted_content"] = "opaque"
        invalid = copy.deepcopy(valid)
        invalid[4]["event"]["item"]["encrypted_content"] = "changed"
        self.assertTrue(probe._stream_order_ok("responses", valid))
        self.assertFalse(probe._stream_order_ok("responses", invalid))
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, invalid, False, 0, False))
        self.assertIn("stream_reasoning_snapshot_mismatch", turn.parse_errors)

    def test_responses_reasoning_terminal_snapshot_preserves_part_types(self) -> None:
        invalid = _response_reasoning_stream()
        invalid[4]["event"]["item"]["summary"][0]["type"] = "text"
        self.assertFalse(probe._stream_order_ok("responses", invalid))
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, invalid, False, 0, False))
        self.assertIn("terminal_output_mismatch", turn.parse_errors)

    def test_responses_stream_rejects_malformed_terminal_encrypted_snapshot(self) -> None:
        events = _response_reasoning_stream()
        events[-1]["event"]["response"]["output"][0]["encrypted_content"] = []
        self.assertFalse(probe._stream_order_ok("responses", events))
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, events, False, 0, False))
        self.assertIn("encrypted_reasoning_snapshot_invalid", turn.parse_errors)

    def test_responses_stream_created_snapshot_requires_object(self) -> None:
        for response in ([], {"object": "other"}):
            invalid = _response_message_stream()
            invalid[0]["event"]["response"] = response
            self.assertFalse(probe._stream_order_ok("responses", invalid))
            turn = probe._parse_responses_stream(probe.TransportResult(200, None, invalid, False, 0, False))
            self.assertIn("response_start_snapshot_invalid", turn.parse_errors)

    def test_responses_stream_created_snapshot_requires_in_progress_empty_output(self) -> None:
        invalid = _response_message_stream()
        invalid[0]["event"]["response"]["output"] = [{"id": "msg_0", "type": "message"}]
        self.assertFalse(probe._stream_order_ok("responses", invalid))
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, invalid, False, 0, False))
        self.assertIn("response_start_snapshot_invalid", turn.parse_errors)

    def test_chat_nonstream_requires_envelope_discriminator(self) -> None:
        valid = {"object": "chat.completion", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}
        self.assertNotIn("chat_object_invalid", probe._parse_chat_document(valid).parse_errors)
        for object_value in (None, "chat.chunk", []):
            document = copy.deepcopy(valid)
            if object_value is None:
                document.pop("object")
            else:
                document["object"] = object_value
            self.assertIn("chat_object_invalid", probe._parse_chat_document(document).parse_errors)

    def test_chat_stream_event_discriminator_must_be_string(self) -> None:
        for event_type in ([], {}):
            events = [{"kind": "event", "type": event_type, "event": {"type": event_type, "choices": []}}]
            self.assertFalse(probe._stream_order_ok("chat", events))
            turn = probe._parse_chat_stream(probe.TransportResult(200, None, events, False, 0, False))
            self.assertIn("stream_event_type_invalid", turn.parse_errors)

    def test_chat_stream_rejects_unknown_event_discriminator(self) -> None:
        events = [
            {"kind": "event", "type": "future_bogus", "event": {"object": "chat.completion.chunk", "type": "future_bogus", "choices": []}},
        ]
        self.assertFalse(probe._stream_order_ok("chat", events))
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, events, False, 0, False))
        self.assertIn("stream_event_unknown", turn.parse_errors)

    def test_chat_stream_error_payload_is_not_a_completion(self) -> None:
        events = [
            {"kind": "event", "event": {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}}]}},
            {"kind": "event", "type": "error", "event": {"object": "chat.completion.chunk", "type": "error", "error": {"type": "server_error"}, "choices": []}},
            {"kind": "event", "event": {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}},
            {"kind": "done"},
        ]
        self.assertFalse(probe._stream_order_ok("chat", events))
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, events, True, 0, False))
        self.assertIn("stream_failure_event", turn.parse_errors)

    def test_chat_stream_error_discriminator_is_not_a_completion(self) -> None:
        events = [
            {"kind": "event", "event": {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}}]}},
            {"kind": "event", "type": "error", "event": {"object": "chat.completion.chunk", "type": "error", "choices": []}},
            {"kind": "event", "event": {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}},
            {"kind": "done"},
        ]
        self.assertFalse(probe._stream_order_ok("chat", events))
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, events, True, 0, False))
        self.assertIn("stream_failure_event", turn.parse_errors)

    def test_chat_stream_requires_explicit_choice_indexes(self) -> None:
        events = [
            {"kind": "event", "event": {"object": "chat.completion.chunk", "choices": [{"delta": {"role": "assistant"}}]}},
        ]
        self.assertFalse(probe._stream_order_ok("chat", events))
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, events, True, 0, False))
        self.assertIn("choice_index_invalid", turn.parse_errors)

    def test_chat_stream_rejects_duplicate_terminal_usage(self) -> None:
        events = [
            {"kind": "event", "sequence": 0, "event": {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}},
            {"kind": "event", "sequence": 1, "event": {"object": "chat.completion.chunk", "choices": [], "usage": {"completion_tokens": 1}}},
            {"kind": "event", "sequence": 2, "event": {"object": "chat.completion.chunk", "choices": [], "usage": {"completion_tokens": 2}}},
            {"kind": "done", "sequence": 3},
        ]
        self.assertFalse(probe._stream_order_ok("chat", events))
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, events, True, 0, False))
        self.assertIn("usage_duplicate", turn.parse_errors)

    def test_responses_done_message_snapshots_are_checked_per_item(self) -> None:
        message_one = {"id": "msg_1", "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": ""}]}
        message_two = {"id": "msg_2", "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "AB"}]}
        events = _responses_wire(
            {"type": "response.created", "response": {}},
            {"type": "response.output_item.added", "output_index": 0, "item": {"id": "msg_1", "type": "message", "role": "assistant"}},
            {"type": "response.content_part.added", "item_id": "msg_1", "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": ""}},
            {"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 0, "content_index": 0, "delta": "A"},
            {"type": "response.output_text.done", "item_id": "msg_1", "output_index": 0, "content_index": 0, "text": "A"},
            {"type": "response.content_part.done", "item_id": "msg_1", "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": "A"}},
            {"type": "response.output_item.done", "output_index": 0, "item": message_one},
            {"type": "response.output_item.added", "output_index": 1, "item": {"id": "msg_2", "type": "message", "role": "assistant"}},
            {"type": "response.content_part.added", "item_id": "msg_2", "output_index": 1, "content_index": 0, "part": {"type": "output_text", "text": ""}},
            {"type": "response.output_text.delta", "item_id": "msg_2", "output_index": 1, "content_index": 0, "delta": "B"},
            {"type": "response.output_text.done", "item_id": "msg_2", "output_index": 1, "content_index": 0, "text": "B"},
            {"type": "response.content_part.done", "item_id": "msg_2", "output_index": 1, "content_index": 0, "part": {"type": "output_text", "text": "B"}},
            {"type": "response.output_item.done", "output_index": 1, "item": message_two},
            {"type": "response.completed", "response": {"status": "completed", "output": [message_one, message_two]}},
        )
        self.assertFalse(probe._stream_order_ok("responses", events))
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, events, False, 0, False))
        self.assertIn("stream_text_snapshot_mismatch", turn.parse_errors)

    def test_anthropic_stream_requires_compatible_stop_sequence(self) -> None:
        valid = [
            {
                "kind": "event",
                "sequence": 0,
                "type": "message_start",
                "event": {
                    "type": "message_start",
                    "message": {
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                    },
                },
            },
            {
                "kind": "event",
                "sequence": 1,
                "type": "message_delta",
                "event": {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}},
            },
            {"kind": "event", "sequence": 2, "type": "message_stop", "event": {"type": "message_stop"}},
        ]
        malformed = copy.deepcopy(valid)
        malformed[1]["event"]["delta"]["stop_sequence"] = []
        self.assertTrue(probe._stream_order_ok("anthropic", valid))
        self.assertFalse(probe._stream_order_ok("anthropic", malformed))
        turn = probe._parse_anthropic_stream(probe.TransportResult(200, None, malformed, False, 0, False))
        self.assertIn("message_delta_stop_sequence_invalid", turn.parse_errors)

    def test_chat_stream_completion_id_must_remain_stable(self) -> None:
        events = [
            {
                "kind": "event",
                "sequence": 0,
                "type": None,
                "event": {
                    "id": "chatcmpl_1",
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}}],
                },
            },
            {
                "kind": "event",
                "sequence": 1,
                "type": None,
                "event": {
                    "id": "chatcmpl_1",
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                },
            },
            {"kind": "done", "sequence": 2, "type": None},
        ]
        self.assertTrue(probe._stream_order_ok("chat", events))
        mismatched = copy.deepcopy(events)
        mismatched[1]["event"]["id"] = "chatcmpl_2"
        self.assertFalse(probe._stream_order_ok("chat", mismatched))
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, mismatched, True, 0, False))
        self.assertIn("stream_completion_id_changed", turn.parse_errors)
        missing = copy.deepcopy(events)
        for item in missing[:-1]:
            item["event"].pop("id")
        self.assertFalse(probe._stream_order_ok("chat", missing))
        missing_turn = probe._parse_chat_stream(probe.TransportResult(200, None, missing, True, 0, False))
        self.assertIn("stream_completion_id_invalid", missing_turn.parse_errors)

    def test_first_turn_rejects_user_scope_leakage(self) -> None:
        turn = probe._parse_anthropic_document(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": probe.USER_SCOPE_LEAK}],
                "stop_reason": "tool_use",
            }
        )
        checks = probe._validate_first(turn, (), stream=False)
        self.assertFalse(checks["checks"]["user_scope"])

    def test_second_turn_rejects_prior_turn_reasoning_leakage(self) -> None:
        first = probe._parse_anthropic_document(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": "PRIVATE_THOUGHT", "signature": "sig"}],
                "stop_reason": "tool_use",
            }
        )
        second = probe._parse_anthropic_document(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "answer PRIVATE_THOUGHT"}],
                "stop_reason": "end_turn",
            }
        )
        checks = probe._validate_second(
            second,
            (),
            stream=False,
            prior_reasoning_parts=first.reasoning_parts,
        )
        self.assertFalse(checks["checks"]["reasoning_not_visible"])


if __name__ == "__main__":
    unittest.main()
