from __future__ import annotations

import ast
import json
from dataclasses import replace
from itertools import product
from pathlib import Path

import pytest

from core.handlers.model_hub.stream_wire import (
    PROTOCOL_STREAM_TAXONOMY,
    SSE_MAX_FRAME_BYTES,
    SSE_MAX_LINE_BYTES,
    SSE_LINE_ENDINGS,
    SSEFrameLimitError,
    SSEFrameTokenizer,
    ProtocolObservation,
    ProtocolTerminalEnvelope,
    ProtocolSSEState,
    ProtocolModelOutputEnvelope,
    ProtocolStreamTaxonomy,
    observe_protocol_response,
)
from core.handlers.model_hub.adapter import RawCallOutcome, RawOutcomeKind
from core.handlers.model_hub.classification import (
    MACHINE_ERROR_CODES,
    UPSTREAM_MACHINE_ERROR_CODES,
    _MACHINE_ERROR_TAXONOMY,
    classify_outcome,
)
from core.handlers.model_hub.events import RETIRED_PERSISTED_REASON_DEGRADATIONS


ROOT = Path(__file__).parents[1]
DEEP_JSON_ARRAY = b"[" * 10_000 + b"0" + b"]" * 10_000
TURN_GATEWAY = ROOT / "core/handlers/model_hub/turn_gateway.py"
SERVICE = ROOT / "core/handlers/model_hub/service.py"
USAGE = ROOT / "core/handlers/model_hub/usage.py"
RPC = ROOT / "core/handlers/model_hub/rpc.py"
V2_CONFIG = ROOT / "config/v2_config.py"
PROVENANCE = ROOT / "core/handlers/model_hub/provenance.py"
ROUTER = ROOT / "modules/agents/model_hub.py"
ADAPTER = ROOT / "vibe/model_hub_runtime/adapter.py"
CLIENT = ROOT / "vibe/model_hub_runtime/client.py"
FIXTURES = Path(__file__).parent / "fixtures" / "model_hub"
STREAM_TRANSPORT_FIXTURES = json.loads((FIXTURES / "stream_transport_boundaries.json").read_text(encoding="utf-8"))[
    "cases"
]


TERMINAL_SETTLEMENT_FIXTURES = json.loads(
    (FIXTURES / "terminal_settlement_boundaries.json").read_text(encoding="utf-8")
)["cases"]
RELEASED_V5_PERMISSION_DENIED = json.loads(
    (FIXTURES / "released_v5_permission_denied.json").read_text(encoding="utf-8")
)
E64_SETTLEMENT_BOUNDARIES = json.loads((FIXTURES / "e64_settlement_boundaries.json").read_text(encoding="utf-8"))

# Owner rulings 19:50-00:06: guard expectations must never derive from guarded code.
# Wire sources:
# - https://platform.claude.com/docs/en/build-with-claude/streaming
# - https://platform.openai.com/docs/api-reference/responses-streaming
# - https://platform.openai.com/docs/api-reference/chat/create#chat-create-stream
STREAM_ENVELOPE_FIXTURES = (
    {
        "protocol": "anthropic",
        "terminal_fact": "served",
        "event_name": "message_stop",
        "selector_path": ("type",),
        "selector_value": "message_stop",
        "error_paths": (),
        "payload": b'{"type":"message_stop"}',
        "source": "https://platform.claude.com/docs/en/build-with-claude/streaming",
    },
    {
        "protocol": "anthropic",
        "terminal_fact": "failed_terminal",
        "event_name": "error",
        "selector_path": ("type",),
        "selector_value": "error",
        "error_paths": (("error",),),
        "payload": b'{"type":"error","error":{"type":"permission_error"}}',
        "source": "https://platform.claude.com/docs/en/build-with-claude/streaming#error-events",
    },
    {
        "protocol": "openai_responses",
        "terminal_fact": "served",
        "event_name": "response.completed",
        "selector_path": ("type",),
        "selector_value": "response.completed",
        "error_paths": (),
        "payload": b'{"type":"response.completed"}',
        "source": "https://platform.openai.com/docs/api-reference/responses-streaming/response/completed",
    },
    {
        "protocol": "openai_responses",
        "terminal_fact": "failed_terminal",
        "event_name": "error",
        "selector_path": ("type",),
        "selector_value": "error",
        "error_paths": ((),),
        "payload": b'{"type":"error","code":"permission_error"}',
        "source": "https://platform.openai.com/docs/api-reference/responses-streaming/error",
    },
    {
        "protocol": "openai_responses",
        "terminal_fact": "failed_terminal",
        "event_name": "response.failed",
        "selector_path": ("type",),
        "selector_value": "response.failed",
        "error_paths": (("response", "error"),),
        "payload": b'{"type":"response.failed","response":{"error":{"code":"permission_error"}}}',
        "source": "https://platform.openai.com/docs/api-reference/responses-streaming/response/failed",
    },
    {
        "protocol": "openai_responses",
        "terminal_fact": "failed_terminal",
        "event_name": "response.incomplete",
        "selector_path": ("type",),
        "selector_value": "response.incomplete",
        "error_paths": (("response", "error"),),
        "required_error_path": ("response", "error"),
        "required_error_code_path": ("response", "error", "code"),
        "payload": b'{"type":"response.incomplete","response":{"error":{"code":"permission_error"}}}',
        "source": "https://platform.openai.com/docs/api-reference/responses-streaming/response/incomplete",
    },
    {
        "protocol": "openai_responses",
        "terminal_fact": "served",
        "event_name": "response.incomplete",
        "selector_path": ("type",),
        "selector_value": "response.incomplete",
        "error_paths": (),
        "payload": b'{"type":"response.incomplete","response":{"incomplete_details":{"reason":"max_output_tokens"}}}',
        "source": "https://platform.openai.com/docs/api-reference/responses-streaming/response/incomplete",
    },
    {
        "protocol": "openai_chat",
        "terminal_fact": "served",
        "event_name": None,
        "literal": b"[DONE]",
        "payload": b"[DONE]",
        "source": "https://developers.openai.com/api/reference/resources/chat",
    },
    {
        "protocol": "openai_chat",
        "terminal_fact": "failed_terminal",
        "event_name": None,
        "selector_path": ("error",),
        "selector_value": None,
        "error_paths": (("error",),),
        "payload": b'{"error":{"type":"permission_error"}}',
        "source": "https://developers.openai.com/api/reference/resources/chat",
    },
)
MODEL_OUTPUT_ENVELOPE_FIXTURES = (
    ("anthropic", "content_block_start", ("type",), "content_block_start", False),
    ("anthropic", "content_block_delta", ("type",), "content_block_delta", False),
    ("openai_responses", "response.output_text.delta", ("type",), "response.output_text.delta", False),
    ("openai_responses", "response.refusal.delta", ("type",), "response.refusal.delta", False),
    (
        "openai_responses",
        "response.reasoning_summary_text.delta",
        ("type",),
        "response.reasoning_summary_text.delta",
        False,
    ),
    (
        "openai_responses",
        "response.function_call_arguments.delta",
        ("type",),
        "response.function_call_arguments.delta",
        False,
    ),
    ("openai_chat", None, ("choices", "*", "delta", "content"), None, True),
    ("openai_chat", None, ("choices", "*", "delta", "refusal"), None, True),
    ("openai_chat", None, ("choices", "*", "delta", "tool_calls"), None, True),
    ("openai_chat", None, ("choices", "*", "delta", "function_call"), None, True),
)
BUFFERED_ERROR_TRUST_ROOT_FIXTURES = {
    protocol: (("error",),) for protocol in ("anthropic", "openai_responses", "openai_chat")
}
ACCEPTED_SSE_LINE_ENDINGS = (b"\r\n", b"\n", b"\r")
STREAM_BOUNDARY_DIMENSIONS = {
    "transport_event": ("eof", "client_error"),
    "settlement_state": ("pending", "served", "failed_terminal"),
    "line_ending": ACCEPTED_SSE_LINE_ENDINGS,
}
STREAM_BOUNDARY_CASES = tuple(
    (protocol, transport_event, settlement_state, line_ending, event_name, payload)
    for protocol in BUFFERED_ERROR_TRUST_ROOT_FIXTURES
    for transport_event, settlement_state, line_ending in product(*STREAM_BOUNDARY_DIMENSIONS.values())
    for event_name, payload in (
        ((None, b"{}"),)
        if settlement_state == "pending"
        else tuple(
            (fixture["event_name"], fixture["payload"])
            for fixture in STREAM_ENVELOPE_FIXTURES
            if fixture["protocol"] == protocol
            and fixture["terminal_fact"] == settlement_state
        )
    )
)
# Review 4913029024: each family must behave identically when its machine fact
# arrives in a buffered error body or a protocol-native stream error event.
MACHINE_ERROR_FAMILY_FIXTURES = {
    "auth": (
        "authentication_error",
        "invalid_api_key",
        "account_banned",
        "account_disabled",
        "account_suspended",
    ),
    "request": (
        "request_too_large",
        "invalid_parameter",
        "invalid_request_error",
        "validation_error",
        "context_length_exceeded",
        "model_not_found",
        "not_found_error",
    ),
    "server": ("server_error", "internal_error", "api_error"),
    "transient": (
        "rate_limit_error",
        "rate_limit_exceeded",
        "quota_exceeded",
        "insufficient_quota",
        "billing_error",
        "overloaded",
        "overloaded_error",
    ),
    "terminal": ("permission_error",),
}
ERROR_TRANSPORT_SHAPES = ("buffered", "streamed")


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _functions(tree: ast.AST) -> dict[str, ast.AST]:
    return {node.name: node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _is_cancel(node: ast.ExceptHandler) -> bool:
    return (
        isinstance(node.type, ast.Attribute)
        and isinstance(node.type.value, ast.Name)
        and node.type.value.id == "asyncio"
        and node.type.attr == "CancelledError"
    )


def _owner_name(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str | None:
    owner = parents.get(node)
    while owner is not None and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
        owner = parents.get(owner)
    return owner.name if owner is not None else None


def _call_name(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _bool_keyword(call: ast.Call, name: str) -> bool | None:
    return next(
        (
            keyword.value.value
            for keyword in call.keywords
            if keyword.arg == name
            and isinstance(keyword.value, ast.Constant)
            and isinstance(keyword.value.value, bool)
        ),
        None,
    )


def _protocol_outcome_calls(tree: ast.AST) -> tuple[ast.Call, ...]:
    return tuple(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _call_name(node) == "_outcome"
        and any(
            keyword.arg == "kind"
            and isinstance(keyword.value, ast.Attribute)
            and keyword.value.attr in {"SUCCESS", "HTTP_ERROR", "PROTOCOL_ERROR"}
            for keyword in node.keywords
        )
    )


def _raw_outcome_constructors(tree: ast.AST) -> tuple[ast.Call, ...]:
    return tuple(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _call_name(node) == "RawCallOutcome"
    )


def _ledger_writes(tree: ast.AST) -> tuple[ast.Attribute, ...]:
    """Every reference to the usage ledger's write, called or handed to a thread."""

    return tuple(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "record"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "usage"
    )


def _auth_status_branch(node: ast.AST) -> bool:
    if not isinstance(node, ast.Compare):
        return False
    left = node.left
    is_status = isinstance(left, ast.Name) and left.id in {"status", "http_status"}
    is_status = is_status or (isinstance(left, ast.Attribute) and left.attr == "status")
    return is_status and any(
        isinstance(value, ast.Constant) and value.value in {401, 403} for value in node.comparators
    )


def test_g1_cancelled_error_has_one_boundary_owner() -> None:
    # A new ``except asyncio.CancelledError`` outside _handle_request must fail.
    tree = _tree(TURN_GATEWAY)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    found = [
        _owner_name(node, parents)
        for node in ast.walk(tree)
        if isinstance(node, ast.Try) and any(_is_cancel(h) for h in node.handlers)
    ]
    assert found == ["_handle_request"]
    terminalizer = _functions(_tree(PROVENANCE))["mark_downstream_canceled"]
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "clear_prepared_attempt"
        for node in ast.walk(terminalizer)
    )


def test_g2_outcome_reads_have_one_settlement_owner() -> None:
    # A second ``await handle.outcome()`` is a deliberate structural violation.
    tree = _tree(TURN_GATEWAY)
    owner = _functions(tree)["_settle_consumed_handle"]
    calls = {
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "outcome"
    }
    owned = {
        node.lineno
        for node in ast.walk(owner)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "outcome"
    }
    assert calls == owned
    source = ast.get_source_segment(TURN_GATEWAY.read_text(encoding="utf-8"), owner)
    assert source is not None
    # Owner ruling 09:44: t0 resources -> t2 protocol terminal -> t3 close/finally
    # commits outcome -> t4 settlement/history -> t5 render -> t6 downstream EOF.
    close_at = source.index("await handle.close_stream()")
    available_at = source.index("handle.outcome_available")
    outcome_at = source.index("await handle.outcome()")
    settlement_at = source.index("self.service.settle_handle_outcome")
    assert close_at < available_at < outcome_at < settlement_at
    response_owner = ast.get_source_segment(
        TURN_GATEWAY.read_text(encoding="utf-8"),
        _functions(tree)["_resolved_response"],
    )
    assert response_owner is not None
    stream_at = response_owner.index("response = web.StreamResponse")
    settle_at = response_owner.index("await self._settle_turn_handle", stream_at)
    render_at = response_owner.index("await self._write_stream_terminal_copy", settle_at)
    eof_at = response_owner.index("await self._downstream_io(response.write_eof())", render_at)
    assert settle_at < render_at < eof_at


def test_g3_settlement_returns_are_consumed() -> None:
    # Replacing an assignment with a bare ``await self._settle_turn_handle(...)`` must fail.
    tree = _tree(TURN_GATEWAY)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Call)
            or not isinstance(node.func, ast.Attribute)
            or node.func.attr not in {"_settle_turn_handle", "settle_handle_outcome"}
        ):
            continue
        parent = parents[node]
        parent = parents[parent] if isinstance(parent, ast.Await) else parent
        assert not isinstance(parent, ast.Expr), node.lineno
    boundary = _functions(tree)["_handle_request"]
    source = ast.get_source_segment(TURN_GATEWAY.read_text(encoding="utf-8"), boundary)
    assert source is not None
    # C9 behavior guard: request cancellation precedes owned teardown, which
    # precedes owned settlement; both drains ignore repeated caller cancellation.
    cancel_at = source.index("request_task.cancel()")
    exit_at = source.index("resources.__aexit__", cancel_at)
    settle_at = source.index("self._settle_boundary_termination", exit_at)
    reraise_at = source.index("raise cancelled", settle_at)
    assert cancel_at < exit_at < settle_at < reraise_at


def test_settlement_generations_are_reserved_only_at_attempt_start() -> None:
    allowed_owners = {
        SERVICE: {"probe_agent", "resolve"},
        ROUTER: {"resolve"},
    }
    for path, expected in allowed_owners.items():
        tree = _tree(path)
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        owners = {
            _owner_name(node, parents)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and _call_name(node) == "_reserve_settlement_generation"
        }
        assert owners == expected

    settlement_owner = _functions(_tree(SERVICE))["_settle_fallback_source"]
    assert not any(
        isinstance(node, ast.Call)
        and _call_name(node) == "_reserve_settlement_generation"
        for node in ast.walk(settlement_owner)
    )


def test_terminal_projection_commit_and_render_have_one_choke_point() -> None:
    # Review 4913624792: render-before-commit and orphaned projections must fail.
    tree = _tree(TURN_GATEWAY)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    owner = _functions(tree)["_commit_and_render_turn_outcome"]
    owned = set(ast.walk(owner))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
            if isinstance(node.func, ast.Attribute)
            else ""
        )
        if name in {"project_turn_outcome_copy", "render_turn_outcome_copy", "record_turn_outcome"}:
            assert node in owned, (name, node.lineno)
    source = ast.get_source_segment(TURN_GATEWAY.read_text(encoding="utf-8"), owner)
    assert source is not None
    commit_at = source.index("terminalizer.record_turn_outcome")
    project_at = source.index("project_turn_outcome_copy")
    render_at = source.index("render_turn_outcome_copy")
    assert commit_at < project_at < render_at


def test_first_model_output_has_one_table_backed_owner() -> None:
    # Review 4913624792: transport/error frames cannot set stream_started.
    stream_tree = _tree(ROOT / "core/handlers/model_hub/stream_wire.py")
    owner = _functions(stream_tree)["is_protocol_model_output"]
    source = ast.get_source_segment(
        (ROOT / "core/handlers/model_hub/stream_wire.py").read_text(encoding="utf-8"),
        owner,
    )
    assert source is not None and "model_output_envelopes" in source
    for name in ("_response_stream", "_observed_stream_terminal_outcome"):
        client_owner = _functions(_tree(CLIENT))[name]
        assert not any(
            isinstance(node, ast.keyword)
            and node.arg == "stream_started"
            and isinstance(node.value, ast.Constant)
            and node.value.value is True
            for node in ast.walk(client_owner)
        )
    stream_source = (ROOT / "core/handlers/model_hub/stream_wire.py").read_text(
        encoding="utf-8"
    )
    client_source = CLIENT.read_text(encoding="utf-8")
    assert "completion_observed" not in stream_source
    assert "allow_completion" not in client_source


def test_protocol_observation_and_outcome_reduction_have_one_owner() -> None:
    # Review 4914187655: buffered and streamed facts cannot bypass observation.
    client_tree = _tree(CLIENT)
    stream_tree = _tree(ROOT / "core/handlers/model_hub/stream_wire.py")
    owners = (_functions(client_tree)["invoke"], _functions(stream_tree)["_observe_frame"])
    calls = [node for tree in (client_tree, stream_tree) for node in ast.walk(tree) if _call_name(node) == "observe_protocol_response"]
    assert all(any(call in set(ast.walk(owner)) for owner in owners) for call in calls)
    assert {value for call in calls if (value := _bool_keyword(call, "streamed")) is not None} == {
        case["stream"] for case in STREAM_TRANSPORT_FIXTURES
    }
    reducer = _functions(client_tree)["_reduce_protocol_observation"]
    assert all(call in set(ast.walk(reducer)) for call in _protocol_outcome_calls(client_tree))
    constructor_owner = _functions(client_tree)["_outcome"]
    assert all(
        call in set(ast.walk(constructor_owner))
        for call in _raw_outcome_constructors(client_tree)
    )


def test_protocol_outcome_owner_guard_rejects_each_bypassed_kind() -> None:
    for kind in ("SUCCESS", "HTTP_ERROR", "PROTOCOL_ERROR"):
        tree = ast.parse(f"_outcome(kind=RawOutcomeKind.{kind})")
        assert len(_protocol_outcome_calls(tree)) == 1
    assert len(
        _raw_outcome_constructors(
            ast.parse("RawCallOutcome(kind=RawOutcomeKind.HTTP_ERROR)")
        )
    ) == 1


def test_usage_metering_has_one_owner_per_call_population() -> None:
    # Review 4958923279 finding 3: a turn can make several upstream calls, and the
    # gateway only ever sees the one whose body it forwards. Metering therefore has
    # two owners over populations split by ``handle.stream is not None``, and a
    # third write anywhere — or a second caller of either owner — would double
    # count or silently drop a billed call.
    service_tree = _tree(SERVICE)
    gateway_tree = _tree(TURN_GATEWAY)
    recorder = _functions(gateway_tree)["_record_usage"]
    # The gateway owner is a pair: `_record_usage` decides, `_persist_usage` carries
    # the write off the loop so no turn's cancellation can take it. That is still one
    # owner only while the second half has exactly one caller, asserted below.
    persister = _functions(gateway_tree)["_persist_usage"]
    owners = (_functions(service_tree)["_meter_call"], recorder, persister)
    writes = _ledger_writes(service_tree) + _ledger_writes(gateway_tree)
    assert writes
    assert all(any(write in set(ast.walk(owner)) for owner in owners) for write in writes)
    invoke = _functions(service_tree)["_invoke"]
    meter_calls = [
        node for node in ast.walk(service_tree) if _call_name(node) == "_meter_call"
    ]
    assert len(meter_calls) == 1
    assert meter_calls[0] in set(ast.walk(invoke))
    persist_calls = [
        node for node in ast.walk(gateway_tree) if _call_name(node) == "_persist_usage"
    ]
    assert len(persist_calls) == 1
    assert persist_calls[0] in set(ast.walk(recorder))
    # Any of the gateway's endings may report the forwarded call, so exactly-once
    # rests on the write it owns; a caller that pre-checks or clears that handle
    # would move the decision outside the owner.
    handles = [
        node
        for node in ast.walk(gateway_tree)
        if isinstance(node, ast.Attribute) and node.attr == "usage_write"
    ]
    assert handles
    assert all(handle in set(ast.walk(recorder)) for handle in handles)


def test_model_identity_is_decided_only_by_its_owner() -> None:
    # Review 4959575659 finding 11: config, resolution, and metering only agree on
    # what "the same model" is while one function decides it. A module that reaches
    # for the raw bound instead is re-deriving the rule, and the notions drift apart
    # again — that is exactly how one model became two ledger rows.
    for path in (SERVICE, USAGE):
        source = path.read_text(encoding="utf-8")
        assert "canonical_model_id" in source
        assert "MODEL_ID_MAX_LENGTH" not in source
    # Review 4960570946: a third admission path had picked up neither half, so
    # the half that is always safe moved to the one validator every path goes
    # through. The bound cannot follow it there — that validator also loads files
    # older releases wrote — so it stays where a request can be refused.
    config_source = V2_CONFIG.read_text(encoding="utf-8")
    assert "normalized_model_id" in config_source
    assert "MODEL_ID_MAX_LENGTH" not in config_source


def test_the_usage_ledger_is_never_touched_from_the_controller_loop() -> None:
    # Review 4960570946: the read blocks on the same lock the writers hold across
    # fsync, so "off the loop" is a property of the ledger, not of whichever
    # caller happened to remember it. Every RPC entry into it is checked here so
    # the next one cannot quietly be the exception.
    tree = _tree(RPC)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    touches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in {"usage_summary", "usage"}
    ]
    assert touches
    for touch in touches:
        enclosing = touch
        off_loop = False
        while enclosing in parents:
            enclosing = parents[enclosing]
            if _call_name(enclosing) == "to_thread":
                off_loop = True
                break
        assert off_loop, f"{touch.attr} is reached on the controller loop"


def test_g4_terminal_projection_has_no_execution_channel() -> None:
    # Adding ``supply_channel`` to a projection helper must fail this scan.
    for path in (SERVICE, PROVENANCE, ROUTER):
        for fn in _functions(_tree(path)).values():
            calls_producer = any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "produce_turn_outcome"
                for node in ast.walk(fn)
            )
            if calls_producer:
                assert not any(isinstance(node, ast.Name) and node.id == "supply_channel" for node in ast.walk(fn))
            if path == ROUTER and getattr(fn, "name", None) == "_no_candidate_error":
                assert any(
                    isinstance(node, ast.Attribute) and node.attr == "_inspect_terminal_chain" for node in ast.walk(fn)
                )


def test_g5_terminalizer_fail_is_validation_only() -> None:
    # A ``terminalizer.fail`` inside cancellation handling must be rejected.
    tree = _tree(TURN_GATEWAY)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "fail":
            assert any(
                isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                and fn.name == "_run_request_turn"
                and fn.lineno <= node.lineno <= fn.end_lineno
                for fn in ast.walk(tree)
            )


def test_nonstream_transport_cannot_enter_the_sse_parser() -> None:
    # Finding 3763339612: only the stream fixture may reach _response_stream.
    source = CLIENT.read_text(encoding="utf-8")
    nonstream = source[source.index("if not stream:") : source.index("loop = asyncio")]
    assert "_response_stream" not in nonstream
    assert {case["stream"] for case in STREAM_TRANSPORT_FIXTURES} == {False, True}


def test_stream_observer_has_no_content_type_validation_gate() -> None:
    source = CLIENT.read_text(encoding="utf-8")
    invoke = _functions(_tree(CLIENT))["invoke"]
    body = ast.get_source_segment(source, invoke)
    assert body is not None
    assert "_is_event_stream_response" not in body


def test_forwarded_terminal_fact_guards_every_settlement_shape() -> None:
    # Finding 3763339614: the settlement call must pass the tracked terminal fact.
    source = TURN_GATEWAY.read_text(encoding="utf-8")
    assert source.count("await self._write_stream_terminal_copy(") == 1
    assert "forwarded_terminal=wire_state.terminal_outcome" in source
    assert all(
        case["write_terminal"] is (case["forwarded_terminal"] is None and case["settlement"] == "copy")
        for case in TERMINAL_SETTLEMENT_FIXTURES
    )


def test_released_reason_fixture_and_read_degradation_match_both_ways() -> None:
    # Finding 3763339617: released expectations are independent of runtime enums.
    released = {
        RELEASED_V5_PERMISSION_DENIED["provenance"][0]["failed_attempts"][0]["reason"],
        RELEASED_V5_PERMISSION_DENIED["resolution_events"][0]["reason"],
    }
    assert released == set(RETIRED_PERSISTED_REASON_DEGRADATIONS)
    assert set(RETIRED_PERSISTED_REASON_DEGRADATIONS.values()) == {RELEASED_V5_PERMISSION_DENIED["degraded_reason"]}


def test_auth_status_heuristics_are_parser_only() -> None:
    # A status==401 branch outside the parser is a deliberate violation.
    tree = _tree(ADAPTER)
    parser = _functions(tree)["_parse_protocol_authenticated_evidence"]
    for fn in _functions(tree).values():
        if fn is parser:
            continue
        for node in ast.walk(fn):
            assert not _auth_status_branch(node)


def test_machine_error_field_access_has_one_extractor() -> None:
    # A second error-code ``.get`` is a deliberate extraction-owner violation.
    paths = [*sorted((ROOT / "core/handlers/model_hub").glob("*.py")), CLIENT]
    for path in paths:
        if path.name == "migration.py":
            continue  # its ``type`` field is an auth-record kind, not an error code
        tree = _tree(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr == "get" and node.args and isinstance(node.args[0], ast.Constant):
                assert node.args[0].value not in {"type", "code"}, (path, node.lineno)


def test_machine_error_extractor_reads_only_declared_envelope_paths() -> None:
    extractor = _functions(_tree(CLIENT))["_raw_error_fields"]
    source = ast.get_source_segment(CLIENT.read_text(encoding="utf-8"), extractor)
    assert source is not None
    assert "envelope_paths" in source
    assert "for path in envelope_paths" in source
    assert not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not extractor
        for node in ast.walk(extractor)
    )


def test_settlement_abandonment_cannot_overwrite_a_committed_terminal_fact() -> None:
    tree = _tree(TURN_GATEWAY)
    abandon = ast.get_source_segment(
        TURN_GATEWAY.read_text(encoding="utf-8"),
        _functions(tree)["_abandon_owned_task"],
    )
    settle = ast.get_source_segment(
        TURN_GATEWAY.read_text(encoding="utf-8"),
        _functions(tree)["_settle_consumed_handle"],
    )
    assert abandon is not None and settle is not None
    guard_at = abandon.index("execution.terminal_fact_committed")
    engine_down_at = abandon.index("terminalizer.engine_down()")
    finish_at = settle.index("terminalizer.finish_attempt(")
    commit_at = settle.index("execution.terminal_fact_committed = True")
    assert guard_at < engine_down_at
    assert finish_at < commit_at


def test_machine_error_family_product_is_complete_across_transports() -> None:
    expected_codes = {code for codes in MACHINE_ERROR_FAMILY_FIXTURES.values() for code in codes}
    assert expected_codes == set(UPSTREAM_MACHINE_ERROR_CODES)
    assert set(MACHINE_ERROR_CODES) - expected_codes == {"engine_down"}
    observed: set[tuple[str, str, str]] = set()
    for family, codes in MACHINE_ERROR_FAMILY_FIXTURES.items():
        for code, transport in product(codes, ERROR_TRANSPORT_SHAPES):
            assert _MACHINE_ERROR_TAXONOMY[code].family == family
            decision = classify_outcome(
                RawCallOutcome(
                    kind=RawOutcomeKind.HTTP_ERROR,
                    http_status=200 if transport == "streamed" else 400,
                    error_code=code,
                    redacted_message=None,
                    stream_started=transport == "streamed",
                    model_id="model-a",
                    source_id="src_fixture123",
                )
            )
            observed.add((family, code, transport))
            assert decision.reason != "unclassified_error"
            if family == "auth" and code in {"authentication_error", "invalid_api_key"}:
                assert decision.action == "refresh"
                assert decision.reason is None
    assert observed == {
        (family, code, transport)
        for family, codes in MACHINE_ERROR_FAMILY_FIXTURES.items()
        for code, transport in product(codes, ERROR_TRANSPORT_SHAPES)
    }


def _assert_stream_taxonomy_matches(
    protocol: str,
    taxonomy: ProtocolStreamTaxonomy,
) -> None:
    fixtures = tuple(fixture for fixture in STREAM_ENVELOPE_FIXTURES if fixture["protocol"] == protocol)
    expected_envelopes = tuple(
        ProtocolTerminalEnvelope(
            event_name=fixture["event_name"],
            selector_path=fixture["selector_path"],
            selector_value=fixture["selector_value"],
            terminal_outcome=fixture["terminal_fact"],
            error_envelope_paths=fixture["error_paths"],
            required_error_path=fixture.get("required_error_path"),
            required_error_code_path=fixture.get("required_error_code_path"),
        )
        for fixture in fixtures
        if "literal" not in fixture
    )
    literal = next(
        (fixture for fixture in fixtures if "literal" in fixture),
        None,
    )
    assert taxonomy.terminal_envelopes == expected_envelopes
    assert taxonomy.model_output_envelopes == tuple(
        ProtocolModelOutputEnvelope(event_name, selector_path, selector_value, require_nonempty)
        for fixture_protocol, event_name, selector_path, selector_value, require_nonempty in MODEL_OUTPUT_ENVELOPE_FIXTURES
        if fixture_protocol == protocol
    )
    assert taxonomy.success_literal == (None if literal is None else (literal["event_name"], literal["literal"]))
    assert taxonomy.buffered_error_envelope_paths == BUFFERED_ERROR_TRUST_ROOT_FIXTURES[protocol]


def test_stream_authority_and_acceptance_fixtures_match_both_ways() -> None:
    assert set(PROTOCOL_STREAM_TAXONOMY) == {str(fixture["protocol"]) for fixture in STREAM_ENVELOPE_FIXTURES}
    assert set(SSE_LINE_ENDINGS) == set(ACCEPTED_SSE_LINE_ENDINGS)
    assert all(str(fixture["source"]).startswith("https://") for fixture in STREAM_ENVELOPE_FIXTURES)
    for protocol, taxonomy in PROTOCOL_STREAM_TAXONOMY.items():
        _assert_stream_taxonomy_matches(protocol, taxonomy)


def test_stream_authority_guard_rejects_an_unaccepted_alias() -> None:
    taxonomy = PROTOCOL_STREAM_TAXONOMY["openai_responses"]
    mutated = replace(
        taxonomy,
        terminal_envelopes=taxonomy.terminal_envelopes
        + (
            ProtocolTerminalEnvelope(
                "response.unaccepted",
                ("type",),
                "response.unaccepted",
                "served",
            ),
        ),
    )
    with pytest.raises(AssertionError):
        _assert_stream_taxonomy_matches("openai_responses", mutated)


def test_stream_authority_guard_rejects_an_orphaned_acceptance_fixture() -> None:
    taxonomy = PROTOCOL_STREAM_TAXONOMY["openai_responses"]
    mutated = replace(
        taxonomy,
        terminal_envelopes=taxonomy.terminal_envelopes[:-1],
    )
    with pytest.raises(AssertionError):
        _assert_stream_taxonomy_matches("openai_responses", mutated)


def test_realtime_terminal_is_not_accepted_by_responses_streaming() -> None:
    state = ProtocolSSEState("openai_responses")
    state.observe(b'event: response.done\ndata: {"type":"response.done"}\n\n')
    assert state.terminal_outcome is None


@pytest.mark.parametrize(
    "data",
    (
        b"{",
        b"[]",
        b"null",
        b'{"type":"response.created","type":"response.completed"}',
        b'{"sequence_number":NaN}',
        DEEP_JSON_ARRAY,
    ),
)
def test_malformed_stream_data_is_transparent_to_a_later_terminal(data: bytes) -> None:
    state = ProtocolSSEState("openai_responses")
    state.observe(b"event: response.created\ndata: " + data + b"\n\n")
    state.observe(
        b'event: response.completed\ndata: {"type":"response.completed","sequence_number":1}\n\n'
    )
    assert state.terminal_observation() == ProtocolObservation(outcome="served")


@pytest.mark.parametrize("next_sequence", (1, 0))
def test_responses_sequence_order_is_ignored_for_settlement(next_sequence: int) -> None:
    state = ProtocolSSEState("openai_responses")
    state.observe(b'event: response.created\ndata: {"type":"response.created","sequence_number":1}\n\n')
    state.observe(
        b'event: response.in_progress\ndata: {"type":"response.in_progress","sequence_number":'
        + str(next_sequence).encode()
        + b"}\n\n"
    )
    state.observe(b'event: response.completed\ndata: {"type":"response.completed"}\n\n')
    assert state.terminal_observation() == ProtocolObservation(outcome="served")


def test_responses_sequence_numbers_accept_a_strictly_increasing_stream() -> None:
    state = ProtocolSSEState("openai_responses")
    state.observe(b'event: response.created\ndata: {"type":"response.created","sequence_number":0}\n\n')
    state.observe(
        b'event: response.completed\ndata: {"type":"response.completed","sequence_number":1}\n\n'
    )
    assert state.terminal_observation() == ProtocolObservation(outcome="served")


def test_responses_missing_sequence_is_ignored_for_settlement() -> None:
    state = ProtocolSSEState("openai_responses")
    state.observe(b'event: response.created\ndata: {"type":"response.created"}\n\n')
    state.observe(b'event: response.completed\ndata: {"type":"response.completed"}\n\n')
    assert state.terminal_observation() == ProtocolObservation(outcome="served")


@pytest.mark.parametrize(
    "prefix",
    (
        b": keep-alive\n\n",
        b"event: ping\n\n",
        b'event: future.event\ndata: {"future":"shape"}\n\n',
    ),
)
def test_spec_ignorable_stream_frames_do_not_poison_terminal_proof(prefix: bytes) -> None:
    state = ProtocolSSEState("openai_responses")
    state.observe(prefix)
    state.observe(
        b'event: response.completed\ndata: {"type":"response.completed","sequence_number":0}\n\n'
    )
    assert state.terminal_observation() == ProtocolObservation(outcome="served")


@pytest.mark.parametrize("protocol", tuple(BUFFERED_ERROR_TRUST_ROOT_FIXTURES))
def test_buffered_protocol_observation_classifies_native_error_envelopes(protocol: str) -> None:
    observation = observe_protocol_response(
        protocol,
        streamed=False,
        data=b'{"error":{"type":"permission_error"}}',
    )
    assert observation.outcome == "failed_terminal"
    assert observation.error_envelope_paths == BUFFERED_ERROR_TRUST_ROOT_FIXTURES[protocol]


@pytest.mark.parametrize("protocol", tuple(BUFFERED_ERROR_TRUST_ROOT_FIXTURES))
def test_buffered_protocol_observation_accepts_valid_success(protocol: str) -> None:
    assert observe_protocol_response(protocol, streamed=False, data=b'{"id":"response"}').outcome == "served"


def test_chat_role_metadata_does_not_cross_the_model_output_boundary() -> None:
    state = ProtocolSSEState("openai_chat")
    state.observe(b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n')
    assert state.model_output_started is False


@pytest.mark.parametrize(
    ("event_name", "payload", "expected_outcome"),
    (
        ("error", b'{"type":"error","error":{"code":"permission_error"}}', "failed_terminal"),
        ("response.failed", b'{"type":"response.failed","code":"permission_error","response":{}}', "failed_terminal"),
        ("response.incomplete", b'{"type":"response.incomplete","response":{"error":null}}', "served"),
        ("response.incomplete", b'{"type":"response.incomplete","response":{"error":{}}}', "served"),
    ),
)
def test_responses_terminal_trust_roots_ignore_unofficial_error_locations(
    event_name: str,
    payload: bytes,
    expected_outcome: str,
) -> None:
    state = ProtocolSSEState("openai_responses")
    state.observe(b"event: " + event_name.encode() + b"\ndata: " + payload + b"\n\n")
    assert state.terminal_outcome == expected_outcome
    assert state.error_envelope_paths in {((),), (), (("response", "error"),)}

    from vibe.model_hub_runtime.client import _raw_error_fields

    _error_type, _error_code, candidates = _raw_error_fields(
        state.error_payload or b"",
        state.error_envelope_paths,
    )
    assert "permission_error" not in candidates


def test_sse_tokenizer_bounds_lines_and_frames() -> None:
    with pytest.raises(SSEFrameLimitError, match="line"):
        SSEFrameTokenizer().feed(b"x" * (SSE_MAX_LINE_BYTES + 1))
    tokenizer = SSEFrameTokenizer()
    line = b"x" * SSE_MAX_LINE_BYTES + b"\n"
    for _ in range(SSE_MAX_FRAME_BYTES // SSE_MAX_LINE_BYTES):
        tokenizer.feed(line)
    with pytest.raises(SSEFrameLimitError, match="frame"):
        tokenizer.feed(b"x")


@pytest.mark.parametrize("split_at", (1, 2, 3))
def test_initial_utf8_bom_is_normalized_for_terminal_observation(split_at: int) -> None:
    state = ProtocolSSEState("openai_responses")
    payload = b'\xef\xbb\xbfevent: response.completed\ndata: {"type":"response.completed"}\n\n'
    state.observe(payload[:split_at])
    state.observe(payload[split_at:])
    assert state.terminal_observation() == ProtocolObservation(outcome="served")


def test_complete_frame_after_terminal_cannot_change_the_fact() -> None:
    frames = (
        b"event: ping\n\n",
        b'data: {"type":"response.output_text.delta"}\n\n',
    )
    for frame in frames:
        state = ProtocolSSEState("openai_responses")
        state.observe(b'event: response.completed\ndata: {"type":"response.completed"}\n\n')
        state.observe(frame)
        assert state.terminal_observation() == ProtocolObservation(outcome="served")


@pytest.mark.parametrize(
    "fixture",
    E64_SETTLEMENT_BOUNDARIES["partial_terminals"],
    ids=lambda fixture: fixture["protocol"] + ":" + str(fixture["payload"]),
)
def test_partial_terminal_cannot_be_completed_before_local_error(
    fixture: dict[str, object],
) -> None:
    protocol = str(fixture["protocol"])
    payload = fixture["payload"]
    encoded = (
        payload.encode("utf-8")
        if isinstance(payload, str)
        else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    )
    forwarded = b"data: " + encoded
    tracker = ProtocolSSEState(protocol)
    tracker.observe(forwarded)
    prefix = tracker.invalidate_partial_frame()
    downstream = ProtocolSSEState(protocol)
    downstream.observe(forwarded + prefix)
    assert downstream.terminal_outcome is None

    rendered = PROTOCOL_STREAM_TAXONOMY[protocol].render_terminal_event(
        "modelHub.launch.retry",
        "Retry directly.",
        downstream.next_sequence_number,
    )
    downstream.observe(
        (b"data: " if protocol == "openai_chat" else b"event: error\ndata: ")
        + json.dumps(rendered, separators=(",", ":")).encode("utf-8")
        + b"\n\n"
    )
    assert downstream.terminal_outcome == "failed_terminal"


def test_e64_settlement_boundary_fixture_covers_every_reviewed_authority() -> None:
    assert set(E64_SETTLEMENT_BOUNDARIES) - {"source"} == {
        "partial_terminals",
        "stream_errors",
        "concurrent_cooldown",
        "stopped_after_terminal",
    }


def test_stream_boundary_catalog_exercises_every_enumerated_dimension() -> None:
    seen_states: set[str] = set()
    for protocol, transport_event, settlement_state, line_ending, event_name, data in STREAM_BOUNDARY_CASES:
        state = ProtocolSSEState(protocol)
        event = b"" if event_name is None else b"event: " + event_name.encode() + line_ending
        state.observe(event + b"data: " + data + line_ending + line_ending)
        if transport_event == "client_error":
            state.observe(b"data: truncated")
        seen_states.add(settlement_state)
        assert state.terminal_outcome == (None if settlement_state == "pending" else settlement_state)
    assert seen_states == set(STREAM_BOUNDARY_DIMENSIONS["settlement_state"])
