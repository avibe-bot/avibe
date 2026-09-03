from __future__ import annotations

import ast
import asyncio
import io
import json
import threading
from dataclasses import replace
from itertools import product
from pathlib import Path

import pytest

from core.handlers.model_hub.stream_wire import (
    PROTOCOL_STREAM_TAXONOMY,
    SSE_OBSERVATION_BYTES,
    SSE_OBSERVATION_EVENT_BYTES,
    SSE_OBSERVATION_STRING_BYTES,
    SSE_LINE_ENDINGS,
    SSEFrameTokenizer,
    ProtocolFactProjector,
    ProtocolObservation,
    ProtocolTerminalEnvelope,
    ProtocolSSEState,
    ProtocolModelOutputEnvelope,
    ProtocolStreamTaxonomy,
    ProtocolUsageReport,
    observe_buffered_protocol_response,
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
MIGRATION = ROOT / "core/handlers/model_hub/migration.py"
USAGE = ROOT / "core/handlers/model_hub/usage.py"
RPC = ROOT / "core/handlers/model_hub/rpc.py"
V2_CONFIG = ROOT / "config/v2_config.py"
PROVENANCE = ROOT / "core/handlers/model_hub/provenance.py"
ROUTER = ROOT / "modules/agents/model_hub.py"
ADAPTER = ROOT / "vibe/model_hub_runtime/adapter.py"
CLIENT = ROOT / "vibe/model_hub_runtime/client.py"
HUB_CLIENT = ROOT / "vibe/model_hub_client.py"
UI_SERVER = ROOT / "vibe/ui_server.py"
INVOKE_CONTRACT = ROOT / "core/handlers/model_hub/adapter.py"
# Every name a caller reaches the usage ledger's read through. Two processes
# forbid reaching it the easy way for one reason -- the read blocks on the lock
# the writers hold across an fsync -- so the rule is declared once here instead
# of restated in each loop's scan, where a second reader would escape both.
LEDGER_READ_NAMES = frozenset({"usage_summary"})
LEDGER_TOUCH_NAMES = LEDGER_READ_NAMES | {"usage"}
PRODUCT_PACKAGES = ("core", "config", "modules", "vibe")
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
    (
        "openai_responses",
        "response.image_generation_call.partial_image",
        ("type",),
        "response.image_generation_call.partial_image",
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


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _reaches_the_ledger(node: ast.AST) -> bool:
    # ``self.usage.<anything>`` -- the ledger object itself, not the writer beside
    # it, which is a different property with a different rule.
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "usage"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "self"
    )


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
        and node.attr in {"record", "record_many"}
        and isinstance(node.value, ast.Attribute)
        and node.value.attr in {"usage", "ledger"}
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
    # Settlement now reaches the handle through the one function that meters the
    # turn first, so the t4 slot in this ordering is named by that owner. The
    # ordering it constrains is unchanged: the response exists, then the turn is
    # ended, then the terminal copy renders into it.
    settle_at = response_owner.index("await self._settle_metered_turn", stream_at)
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
    # The engine adapter reads buffered protocol facts once; the gateway consumes
    # its RawCallOutcome instead of independently interpreting the same body.
    client_tree = _tree(CLIENT)
    stream_tree = _tree(ROOT / "core/handlers/model_hub/stream_wire.py")
    gateway_tree = _tree(TURN_GATEWAY)
    stream_calls = [
        node
        for tree in (client_tree, stream_tree)
        for node in ast.walk(tree)
        if _call_name(node) == "observe_protocol_response"
    ]
    assert stream_calls == []
    stream_source = (ROOT / "core/handlers/model_hub/stream_wire.py").read_text(
        encoding="utf-8"
    )
    assert "class ProtocolFactProjector" in stream_source
    assert "observation = frame.observation" in stream_source
    buffered_owner = _functions(client_tree)["invoke"]
    buffered_calls = [
        node
        for tree in (client_tree, gateway_tree)
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and node.id == "observe_buffered_protocol_response"
        and isinstance(node.ctx, ast.Load)
    ]
    assert len(buffered_calls) == 2
    assert all(call in set(ast.walk(buffered_owner)) for call in buffered_calls)
    assert not any(
        isinstance(node, ast.Name)
        and node.id == "observe_buffered_protocol_response"
        for node in ast.walk(gateway_tree)
    )
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
    owners = (_functions(service_tree)["_meter_call"], recorder)
    # Reviews 4964754924 / 4964894667: an owner decides *that* a call is metered,
    # but how long that write outlives whoever queued it is a third property and
    # belongs to neither population. `UsageWriter` owns it once, so neither owner
    # may touch the ledger itself and a population added later inherits the
    # lifetime by construction instead of by remembering to.
    assert not _ledger_writes(service_tree)
    assert not _ledger_writes(gateway_tree)
    writer = next(
        node
        for node in ast.walk(_tree(USAGE))
        if isinstance(node, ast.ClassDef) and node.name == "UsageWriter"
    )
    persist = _functions(writer)["_flush_pending"]
    writes = _ledger_writes(writer)
    assert writes
    assert all(write in set(ast.walk(persist)) for write in writes)
    # Which leaves each owner one job at the writer, and leaves nothing else in
    # either module recording anything at all.
    handoffs = [
        node
        for tree in (service_tree, gateway_tree)
        for node in ast.walk(tree)
        if _call_name(node) == "record"
    ]
    assert handoffs
    assert all(any(handoff in set(ast.walk(owner)) for owner in owners) for handoff in handoffs)
    assert all(
        len([handoff for handoff in handoffs if handoff in set(ast.walk(owner))]) == 1
        for owner in owners
    )
    invoke = _functions(service_tree)["_invoke"]
    meter_calls = [
        node for node in ast.walk(service_tree) if _call_name(node) == "_meter_call"
    ]
    assert len(meter_calls) == 1
    assert meter_calls[0] in set(ast.walk(invoke))
    # Any of the gateway's endings may report the forwarded call, so exactly-once
    # rests on the write it owns; a caller that pre-checks or clears that handle
    # would move the decision outside the owner. An ending may still need to know
    # whether a row is owed — the abandonment path decides whether to run at all —
    # so that question is a property of the turn and the only other reader of the
    # handle. Review 4970...: it used to ask whether the turn was *settled*, which
    # a forced terminal had already made true for a call the vendor billed.
    owes = _functions(gateway_tree)["owes_metering"]
    readers = (recorder, owes)
    handles = [
        node
        for node in ast.walk(gateway_tree)
        if isinstance(node, ast.Attribute) and node.attr == "usage_write"
    ]
    assert handles
    assert all(any(handle in set(ast.walk(reader)) for reader in readers) for handle in handles)
    assert not [
        node
        for node in ast.walk(owes)
        if isinstance(node, ast.Assign) or isinstance(node, ast.AugAssign)
    ]


def test_settling_a_turn_is_what_meters_it_for_every_ending_there_will_be() -> None:
    # Review 4970...: metering was positioned relative to bookkeeping rather than to
    # the upstream call, and each ending paid for it differently — one settled first
    # and skipped the row when settlement raised, one never metered at all. The
    # handle settlement owner is also the sole adapter-outcome reader, so it can
    # record the complete upstream facts immediately before service settlement.
    # An ending added later inherits that order instead of restating it.
    gateway_tree = _tree(TURN_GATEWAY)
    functions = _functions(gateway_tree)
    owner = functions["_settle_consumed_handle"]
    recorded = [node for node in ast.walk(gateway_tree) if _call_name(node) == "_record_usage"]
    assert len(recorded) == 1
    assert recorded[0] in set(ast.walk(owner))
    outcome_reads = [node for node in ast.walk(gateway_tree) if _call_name(node) == "outcome"]
    service_settlements = [node for node in ast.walk(gateway_tree) if _call_name(node) == "settle_handle_outcome"]
    assert len(outcome_reads) == 1
    assert len(service_settlements) == 1
    assert outcome_reads[0] in set(ast.walk(owner))
    assert service_settlements[0] in set(ast.walk(owner))
    assert outcome_reads[0].lineno < recorded[0].lineno < service_settlements[0].lineno
    # And every turn ending still reaches that owner through the single settlement
    # wrapper, so "settled here" cannot come apart from "metered here".
    settlement_wrapper = functions["_settle_metered_turn"]
    handle_wrapper = functions["_settle_turn_handle"]
    settlements = [node for node in ast.walk(gateway_tree) if _call_name(node) == "_settle_turn_handle"]
    assert len(settlements) == 1
    assert settlements[0] in set(ast.walk(settlement_wrapper))
    consumed = [node for node in ast.walk(gateway_tree) if _call_name(node) == "_settle_consumed_handle"]
    assert len(consumed) == 1
    assert consumed[0] in set(ast.walk(handle_wrapper))
    endings = [node for node in ast.walk(gateway_tree) if _call_name(node) == "_settle_metered_turn"]
    assert len(endings) >= 2
    assert all(
        any(ending in set(ast.walk(functions[name])) for name in ("_settle_boundary_termination", "_resolved_response"))
        for ending in endings
    )


def test_a_metered_row_is_dated_by_the_call_and_not_by_what_followed_it() -> None:
    # Review 4970...: the other half of the same class. The recorder read the clock
    # itself, so a row carried the moment bookkeeping got around to it rather than
    # the moment the call ended — across local midnight, the wrong day's usage for a
    # call the vendor billed on the previous one. Asserted here rather than driven,
    # because the fix is what makes it unreachable: nothing suspends between the
    # body ending and the row being queued, so no clock can move in between and no
    # test can make one. What is left to protect is where the instant comes from.
    gateway_tree = _tree(TURN_GATEWAY)
    functions = _functions(gateway_tree)
    # Captured where the upstream body ends, which is the one function that has a
    # body to end. A capture anywhere else is a second answer to when the call
    # finished, and the endings are exactly the places that would get it wrong.
    captures = [
        node
        for node in ast.walk(gateway_tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Attribute) and target.attr == "completed_at"
            for target in node.targets
        )
    ]
    assert captures
    assert all(capture in set(ast.walk(functions["_resolved_response"])) for capture in captures)
    # And carried from there to the ledger rather than re-read at the write.
    handoff = next(
        node for node in ast.walk(functions["_record_usage"]) if _call_name(node) == "record"
    )
    dated = next(keyword for keyword in handoff.keywords if keyword.arg == "at")
    assert any(
        isinstance(node, ast.Attribute) and node.attr == "completed_at"
        for node in ast.walk(dated.value)
    )


def test_how_long_a_turn_waits_on_the_ledger_is_the_writers_to_bound() -> None:
    # Reviews 4964754924 / 4964894667 / 4970...: the wait was spelled at each
    # metering call site, which made the bound the one property a new site could
    # omit by writing the obvious `await` — and one did, putting an unresponsive
    # disk on a turn's critical path and in front of the next failover hop. Both
    # halves, shield and bound, now sit behind one name. The scan keys on the usage
    # write specifically, so an unrelated shielded task is not mistaken for one.
    waited = {"shield", "wait_for"}
    usage_names = {"record", "usage_write", "wait_recorded", "usage_writer"}

    def wraps_a_usage_write(tree: ast.AST) -> list[ast.Call]:
        return [
            node
            for node in ast.walk(tree)
            if _call_name(node) in waited
            and any(
                isinstance(inner, ast.Attribute) and inner.attr in usage_names
                or isinstance(inner, ast.Name) and inner.id in usage_names
                for argument in node.args
                for inner in ast.walk(argument)
            )
        ]

    usage_tree = _tree(USAGE)
    waiter = _functions(usage_tree)["wait_recorded"]
    assert {name for node in ast.walk(waiter) if (name := _call_name(node)) in waited} == waited
    # Inside the writer the wait is spelled over its own parameter, so the name-keyed
    # scan is aimed outward: at the modules that hold a `usage_write` and could wait
    # on it themselves. Every one of them must go through the name above instead.
    assert all(node in set(ast.walk(waiter)) for node in wraps_a_usage_write(usage_tree))
    for module in sorted((ROOT / "core/handlers/model_hub").glob("*.py")):
        if module == USAGE:
            continue
        assert not wraps_a_usage_write(_tree(module)), module.name


def test_every_body_fact_the_turn_reads_can_come_from_the_engine_that_read_it() -> None:
    # Review 4965405530: the gateway forwards a body the engine had already begun
    # reading, and each round of this class was one more fact of that body that
    # existed only in bytes the gateway itself had pulled — so every ending that
    # opens before the first pull answered it wrong, one fact at a time. The
    # property is not which facts those are: it is that a fact read from the
    # gateway's own tracker is a fact about the forwarded body, and every one of
    # them can be answered by the observation that saw the body first.
    execution = next(
        node
        for node in ast.walk(_tree(TURN_GATEWAY))
        if isinstance(node, ast.ClassDef) and node.name == "_TurnExecution"
    )

    def reads(owner: ast.AST, attribute: str) -> bool:
        return any(
            isinstance(node, ast.Attribute)
            and node.attr == attribute
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            for node in ast.walk(owner)
        )

    body_facts = [
        node
        for node in execution.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(decorator, ast.Name) and decorator.id == "property"
            for decorator in node.decorator_list
        )
        and reads(node, "wire_state")
    ]
    assert body_facts
    assert [fact.name for fact in body_facts if not reads(fact, "upstream_observation")] == []

    # Which the engine can only answer if the whole boundary crosses: a member the
    # contract declares and the one real implementation drops is a fact that stops
    # at the hand-off, and the reader above would be asking a fake for it.
    def members(tree: ast.AST, name: str) -> set[str]:
        owner = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == name
        )
        return {
            node.name
            for node in owner.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    declared = members(_tree(INVOKE_CONTRACT), "InvokeHandle")
    assert "observed" in declared
    assert declared <= members(_tree(CLIENT), "EngineInvokeHandle")


def test_model_identity_is_decided_only_by_its_owner() -> None:
    # Review 4959575659 finding 11: config, resolution, and metering only agree on
    # what "the same model" is while one function decides it. A module that reaches
    # for the raw bound instead is re-deriving the rule, and the notions drift apart
    # again — that is exactly how one model became two ledger rows.
    #
    # Review 4965885614: which function is the right one depends on whether the
    # module may answer no. The service admits, so it asks the admission question;
    # the ledger meters a call that already happened, so asking it there dropped
    # every call made under an identifier a legacy file kept loadable. Neither may
    # spell the bound, and neither may borrow the other's question.
    #
    # Review 4966041599: within the ledger the same split appears again by
    # direction. Deriving a key for a call cannot refuse it; accepting a key a row
    # already carries can, and must not re-derive it. One function serving both
    # forced the fold to start late enough to be idempotent, which is what let a
    # legacy identifier occupy a folded key and share another model's row.
    assert "canonical_model_id" in SERVICE.read_text(encoding="utf-8")
    usage_source = USAGE.read_text(encoding="utf-8")
    assert "usage_ledger_key" in usage_source
    assert "persisted_ledger_key" in usage_source
    assert "canonical_model_id" not in usage_source
    for path in (SERVICE, USAGE):
        source = path.read_text(encoding="utf-8")
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
    parents = _parents(tree)
    touches = [node for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr in LEDGER_TOUCH_NAMES]
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


def test_the_ledger_read_is_the_whole_of_what_both_loops_are_told_to_watch() -> None:
    # Review 4966281026: two processes now scan for this read by name, and a
    # second read added to the service would satisfy neither name list and so
    # pass both scans. The list is therefore declared once and asserted here to
    # be the whole of what the service exposes -- a new reader fails next to the
    # declaration that has to grow with it, rather than in a review round.
    readers = {
        name for name, fn in _functions(_tree(SERVICE)).items() if any(map(_reaches_the_ledger, ast.walk(fn)))
    }
    assert readers == set(LEDGER_READ_NAMES)


def test_the_ledger_read_reaches_the_web_ui_awaited_and_off_the_compat_surface() -> None:
    # Review 4966281026 finding 4: the same property as the controller guard
    # above, one process over. The read blocks on the writers' lock across an
    # fsync, so reaching it from the compat surface -- whose handlers are sync and
    # run in a threadpool worker -- occupies a UI worker for as long as the disk
    # takes. Awaiting it on the native surface is what keeps that cost on the
    # event loop's own terms.
    tree = _tree(UI_SERVER)
    parents = _parents(tree)
    touches = [node for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr in LEDGER_TOUCH_NAMES]
    assert touches
    for touch in touches:
        awaited = False
        native = False
        enclosing = touch
        while enclosing in parents:
            enclosing = parents[enclosing]
            if isinstance(enclosing, ast.Await):
                awaited = True
            if isinstance(enclosing, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert isinstance(enclosing, ast.AsyncFunctionDef), f"{enclosing.name} reaches {touch.attr} sync"
                native = native or any(
                    _call_name(node) == "_dispatch_native_ui_request" for node in ast.walk(enclosing)
                )
        assert awaited, f"{touch.attr} is called rather than awaited"
        assert native, f"{touch.attr} is served off the native FastAPI surface"

    # The client the route reaches it through has to keep the same shape: one sync
    # method here would move the block back into a worker with nothing in
    # `ui_server.py` to show for it.
    for name, fn in _functions(_tree(HUB_CLIENT)).items():
        if name not in LEDGER_READ_NAMES:
            continue
        assert isinstance(fn, ast.AsyncFunctionDef), f"{name} is a sync RPC"
        assert not any(_call_name(node) == "_rpc_sync" for node in ast.walk(fn))


def test_a_usage_row_is_labelled_without_its_caller_knowing_how_it_was_keyed() -> None:
    # Review 4966281026 finding 1: a row's key is an identity or a bounded head
    # plus a digest of it, and which one is a fold only this module performs. Three
    # heads running, the reviewer has found a caller keying its own lookup at a
    # new call site each time -- so the class is closed by leaving no caller with
    # a key to join on. The service hands identities down and the ledger keys both
    # sides of the join itself.
    importers = set()
    for package in PRODUCT_PACKAGES:
        for path in (ROOT / package).rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if "usage_ledger_key" not in source:
                continue
            if any(
                isinstance(node, ast.ImportFrom) and any(alias.name == "usage_ledger_key" for alias in node.names)
                for node in ast.walk(ast.parse(source))
            ):
                importers.add(path)
    assert importers == {USAGE}

    reader = _functions(_tree(SERVICE))["usage_summary"]
    # Subscripting a row is how the fold leaked the first two times: the caller
    # read `row["source_id"]` and looked a label up under an identity the row does
    # not carry, reporting every folded row as unlabelled while it still existed.
    assert not [
        node
        for node in ast.walk(reader)
        if isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value in {"source_id", "model_id", "sources", "models"}
    ]
    handoffs = [node for node in ast.walk(reader) if _call_name(node) == "summary"]
    assert len(handoffs) == 1
    assert "identities" in {keyword.arg for keyword in handoffs[0].keywords}
    # Review 4967250750 finding 1: a mapping built here is a join key whose arity the
    # caller chose, and arity is what went wrong on the fourth head. A metered model's
    # identity is the pair (source, model), so a flat ``{model.id: ...}`` answered for
    # a model removed from one Source as long as another Source still listed it.
    # Handing the nesting down as a typed record leaves no arity to get wrong.
    assert not [node for node in ast.walk(reader) if isinstance(node, (ast.Dict, ast.DictComp))]


def test_no_hub_worker_thread_can_be_waited_on_at_process_exit() -> None:
    """Review 4967250750 finding 3: metering must be abandonable, not merely bounded.

    Every wait the hub makes a caller do is bounded, which is what keeps a served turn
    off a disk. Shutdown is where a bound stops being the question: the work is still
    running after the last bounded wait returned, so what matters is whether the
    runtime will walk away from it. `ThreadPoolExecutor` will not — it registers an
    `atexit` hook that joins non-daemon workers — so a worker wedged in `fsync` holds
    interpreter shutdown open forever.

    Scanned over the package rather than asserted about the one class that had the
    defect: a second worker started anywhere in the hub inherits the same rule, which
    is the part nobody remembers when adding one. ``MH-USAGE-015`` proves the
    behaviour; this keeps the next thread from having to rediscover it.
    """

    started = 0
    for path in sorted((ROOT / "core/handlers/model_hub").rglob("*.py")):
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            assert name not in {
                "ThreadPoolExecutor",
                "ProcessPoolExecutor",
            }, f"{path.name} builds a pool whose workers are joined at exit"
            if name != "Thread":
                continue
            started += 1
            daemon = [keyword for keyword in node.keywords if keyword.arg == "daemon"]
            assert len(daemon) == 1, f"{path.name} starts a thread without saying whether it is a daemon"
            assert daemon[0].value.value is True, f"{path.name} starts a thread the runtime will wait on"
    assert started, "no hub worker thread found -- the scan has stopped covering anything"


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


def test_routing_a_gateway_request_and_attributing_it_are_one_critical_section() -> None:
    """Review 5092034678: a turn rotation must not land between the two.

    Where a gateway request goes and which turn may be credited with it are one
    decision about one request. Resolved under one acquisition of the lock and
    bound under another, a turn can settle in the gap: whichever turn becomes
    the sole live one is then told a model it never asked for arrived on its
    token, which marks it ambiguous and drops the provenance of the request it
    goes on to make itself.

    Stated as "whoever routes also attributes, under one held lock" rather than
    naming the owner, so splitting the decision back apart fails here however
    the pieces are renamed or moved.
    """

    attribution = {"begin_gateway_request", "clear_prepared_attempt"}
    routing_owners: set[str] = set()
    for name, fn in _functions(_tree(PROVENANCE)).items():
        routing = [
            node
            for node in ast.walk(fn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_route_gateway_model"
        ]
        if not routing:
            continue
        routing_owners.add(name)
        held = [
            node
            for node in ast.walk(fn)
            if isinstance(node, ast.With)
            and any(
                isinstance(item.context_expr, ast.Attribute) and item.context_expr.attr == "_lock"
                for item in node.items
            )
        ]
        assert len(held) == 1, name
        critical_section = range(held[0].lineno, held[0].end_lineno + 1)
        bound = [
            node
            for node in ast.walk(fn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in attribution
        ]
        assert bound, name
        for node in routing + bound:
            assert node.lineno in critical_section, f"{name}:{node.lineno}"
    # A rename that leaves nothing to scan must fail here, not pass vacuously.
    assert routing_owners


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
    functions = _functions(_tree(CLIENT))
    extractor = functions["_raw_error_fields"]
    projector = functions["_project_raw_error_fields"]
    client_source = CLIENT.read_text(encoding="utf-8")
    extractor_source = ast.get_source_segment(client_source, extractor)
    projector_source = ast.get_source_segment(client_source, projector)
    assert extractor_source is not None
    assert projector_source is not None
    assert "_project_raw_error_fields" in extractor_source
    assert "for envelope_path in envelope_paths" in projector_source
    assert not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not extractor
        for node in ast.walk(extractor)
    )


def test_reasoning_tier_resolver_call_inventory_requires_batch_indexes() -> None:
    expected = {
        ("service.py", "apply_tiers"): True,
        ("service.py", "_create_oauth_source"): True,
        ("service.py", "create_source"): True,
        ("service.py", "_apply_reasoning_tier_ladder"): False,
        ("migration.py", "_validated_source"): True,
        ("migration.py", "apply_native_migration"): True,
    }
    calls_by_owner: dict[tuple[str, str | None], list[ast.Call]] = {}
    for path in (SERVICE, MIGRATION):
        tree = _tree(path)
        parents = _parents(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node) != "resolve_reasoning_tiers":
                continue
            key = (path.name, _owner_name(node, parents))
            calls_by_owner.setdefault(key, []).append(node)

    assert set(calls_by_owner) == set(expected)
    assert all(len(calls) == 1 for calls in calls_by_owner.values())
    for key, needs_batch_index in expected.items():
        [call] = calls_by_owner[key]
        injected = next(
            (
                keyword.value
                for keyword in call.keywords
                if keyword.arg == "catalog_efforts_by_model"
            ),
            None,
        )
        if needs_batch_index:
            assert isinstance(injected, ast.Name)
            assert injected.id == "catalog_efforts_by_model"
        else:
            assert injected is None


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


def test_chat_any_nonempty_choice_crosses_the_model_output_boundary() -> None:
    state = ProtocolSSEState("openai_chat")
    state.observe(
        b'data: {"choices":[{"delta":{"content":"hello"}},'
        b'{"delta":{"content":null}}]}\n\n'
    )

    assert state.model_output_started is True


def test_buffered_error_array_is_not_an_error_envelope() -> None:
    observation = observe_buffered_protocol_response(
        "openai_chat",
        io.BytesIO(b'{"error":[],"choices":[{"message":{"content":"hello"}}]}'),
    )

    assert observation.outcome == "served"
    assert observation.error_envelope_paths == ()


def test_duplicate_buffered_member_replaces_earlier_protocol_facts() -> None:
    observation = observe_buffered_protocol_response(
        "openai_chat",
        io.BytesIO(
            b'{"error":{},"error":[],"usage":{"prompt_tokens":10},'
            b'"usage":{"prompt_tokens":2}}'
        ),
    )

    assert observation.outcome == "served"
    assert observation.error_envelope_paths == ()
    assert observation.usage == ProtocolUsageReport(input_tokens=2)


def test_duplicate_chat_choice_member_replaces_only_its_choice() -> None:
    def observe(payload: bytes) -> bool:
        state = ProtocolSSEState("openai_chat")
        state.observe(b"data: " + payload + b"\n\n")
        return state.model_output_started

    replaced_only = observe(
        b'{"choices":[{"delta":{"content":"hello","content":null}}]}'
    )
    sibling_survives = observe(
        b'{"choices":[{"delta":{"content":"hello"}},'
        b'{"delta":{"content":"stale","content":null}}]}'
    )

    assert replaced_only is False
    assert sibling_survives is True


def test_chat_projection_facts_do_not_grow_with_choice_count() -> None:
    projector = ProtocolFactProjector("openai_chat")
    projector.feed(
        b'{"choices":['
        + b",".join(
            b'{"delta":{"content":"hello"}}' for _index in range(10_000)
        )
        + b"]}"
    )

    assert projector.finish(streamed=True).model_output_started is True
    assert projector._nonempty == {(), ("choices", "*", "delta", "content")}
    assert projector._scoped_nonempty == set()


def test_buffered_machine_codes_are_scoped_to_the_matched_error_envelope() -> None:
    observation = observe_buffered_protocol_response(
        "openai_responses",
        io.BytesIO(
            b'{"error":{"type":"server_error"},'
            b'"response":{"error":{"type":"permission_error"}}}'
        ),
        machine_error_codes=frozenset({"permission_error", "server_error"}),
    )

    assert observation.outcome == "failed_terminal"
    assert observation.error_envelope_paths == (("error",),)
    assert observation.error_type_candidates == ("server_error",)


def test_sse_data_json_accepts_one_leading_utf8_bom() -> None:
    state = ProtocolSSEState("openai_responses")
    state.observe(
        b"event: response.completed\ndata: \xef\xbb\xbf"
        b'{"type":"response.completed"}\n\n'
    )

    assert state.terminal_outcome == "served"


def test_colonless_empty_event_field_uses_default_message_event() -> None:
    state = ProtocolSSEState("openai_chat")
    state.observe(b'event\ndata: {"choices":[{"delta":{"content":"hello"}}]}\n\n')

    assert state.model_output_started is True


def test_large_selected_string_preserves_its_nonempty_fact() -> None:
    state = ProtocolSSEState("openai_chat")
    state.observe(
        b'data: {"choices":[{"delta":{"content":"'
        + (b"x" * (SSE_OBSERVATION_STRING_BYTES + 1))
        + b'"}}]}\n\n'
    )

    assert state.model_output_started is True


def test_large_required_error_code_preserves_its_nonempty_fact() -> None:
    state = ProtocolSSEState("openai_responses")
    state.observe(
        b'event: response.incomplete\ndata: {"type":"response.incomplete",'
        b'"response":{"error":{"code":"'
        + (b"x" * (SSE_OBSERVATION_STRING_BYTES + 1))
        + b'"}}}\n\n'
    )

    assert state.terminal_outcome == "failed_terminal"
    assert state.error_envelope_paths == (("response", "error"),)


def test_async_sse_observation_runs_outside_the_event_loop() -> None:
    state = ProtocolSSEState("openai_responses")
    caller_thread = threading.get_ident()
    observer_threads: list[int] = []
    original_observe = state.observe

    def recording_observe(chunk: bytes) -> None:
        observer_threads.append(threading.get_ident())
        original_observe(chunk)

    state.observe = recording_observe
    asyncio.run(
        state.observe_async(
            b'event: response.completed\ndata: {"type":"response.completed"}\n\n'
        )
    )

    assert observer_threads
    assert observer_threads[0] != caller_thread
    assert state.terminal_outcome == "served"


def test_cancelled_sse_observation_drains_its_worker_before_settlement() -> None:
    async def run() -> None:
        state = ProtocolSSEState("openai_responses")
        entered = threading.Event()
        release = threading.Event()
        original_observe = state.observe

        def blocked_observe(chunk: bytes) -> None:
            entered.set()
            assert release.wait(1)
            original_observe(chunk)

        state.observe = blocked_observe
        observer = asyncio.create_task(
            state.observe_async(
                b'event: response.completed\ndata: {"type":"response.completed"}\n\n'
            )
        )
        assert await asyncio.to_thread(entered.wait, 1)
        observer.cancel()
        await asyncio.sleep(0)
        assert not observer.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await observer
        assert state.terminal_outcome == "served"

    asyncio.run(run())


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


def test_exact_sse_tokenizer_accepts_large_protocol_frames() -> None:
    tokenizer = SSEFrameTokenizer()
    data = b"x" * (2 * 1024 * 1024)

    assert tokenizer.feed(b"data: " + data + b"\n\n") == (b"data: " + data,)


def test_large_image_event_is_observed_without_retaining_its_base64_body() -> None:
    state = ProtocolSSEState("openai_responses")
    event_type = b"response.image_generation_call.partial_image"
    state.observe(
        b"event: " + event_type + b'\ndata: {"type":"' + event_type + b'","partial_image_b64":"'
    )

    assert state.model_output_started is False
    for _ in range(32):
        state.observe(b"x" * (64 * 1024))
        assert state.tokenizer.retained_bytes < SSE_OBSERVATION_STRING_BYTES + 1024

    state.observe(b'"}\n\n')
    assert state.model_output_started is True
    state.observe(
        b'event: response.completed\ndata: {"type":"response.completed",'
        b'"response":{"usage":{"input_tokens":4,"output_tokens":7}}}\n\n'
    )

    assert state.terminal_outcome == "served"
    assert state.usage == ProtocolUsageReport(input_tokens=4, output_tokens=7)


def test_observer_abandons_large_non_string_metadata_without_affecting_next_frame() -> None:
    state = ProtocolSSEState("openai_responses")
    state.observe(b"event: response.output_text.delta\ndata: [" + b"0," * SSE_OBSERVATION_BYTES)

    assert state.model_output_started is False
    assert state.tokenizer.retained_bytes < 1024

    state.observe(
        b"0]\n\nevent: response.completed\ndata: "
        b'{"type":"response.completed"}\n\n'
    )

    assert state.model_output_started is False
    assert state.terminal_observation() == ProtocolObservation(outcome="served")


def test_large_terminal_projects_its_discriminator_after_unrelated_structure() -> None:
    state = ProtocolSSEState("openai_responses")
    payload = json.dumps(
        {
            "output": [{"index": index} for index in range(20_000)],
            "type": "response.completed",
            "response": {"usage": {"input_tokens": 11, "output_tokens": 7}},
        },
        separators=(",", ":"),
    ).encode()

    state.observe(b"event: response.completed\ndata: " + payload + b"\n\n")

    assert state.terminal_outcome == "served"
    assert state.usage == ProtocolUsageReport(input_tokens=11, output_tokens=7)
    assert state.tokenizer.retained_bytes == 0


def test_observer_drops_unrecognized_oversized_event_names() -> None:
    state = ProtocolSSEState("anthropic")
    state.observe(b"event: " + b"x" * (SSE_OBSERVATION_EVENT_BYTES * 4))

    assert state.model_output_started is False
    assert state.tokenizer.retained_bytes <= SSE_OBSERVATION_EVENT_BYTES

    state.observe(b'\ndata: {"type":"content_block_delta"}\n\n')

    assert state.model_output_started is False


@pytest.mark.parametrize(
    "data",
    (
        b"[DONE]",
        b'{"choices":[{"delta":{"content":"hello"}}]}',
    ),
)
def test_oversized_chat_event_name_is_not_the_default_event(data: bytes) -> None:
    state = ProtocolSSEState("openai_chat")
    state.observe(
        b"event: "
        + b"x" * (SSE_OBSERVATION_EVENT_BYTES + 1)
        + b"\ndata: "
        + data
        + b"\n\n"
    )

    assert state.terminal_observation() is None
    assert state.model_output_started is False
    assert state.tokenizer.retained_bytes == 0


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


def test_wire_bytes_reach_the_observer_before_the_buffer_that_can_refuse_them() -> None:
    """Review 4965677908: a full prelude must not erase a report it received.

    Two questions get asked about the same bytes and only the second can fail:
    what they say, and whether there is room to keep a replay of them. Every
    arrival site therefore hands them to one owner, and that owner reads before
    it stores — so a site added later cannot reintroduce the order that drops a
    usage frame the vendor already billed.
    """

    source = CLIENT.read_text(encoding="utf-8")
    assert source.count("prelude.write_async(") == 1
    owner = ast.get_source_segment(source, _functions(_tree(CLIENT))["_received"])
    assert owner is not None
    assert "prelude.write_async(" in owner
    assert owner.index("wire_state.observe_async(") < owner.index("prelude.write_async(")


def test_no_model_hub_module_spells_its_own_atomic_replacement() -> None:
    """Review 4965677908: a failed replacement must not leave a temp file behind.

    Cleanup is unobservable from outside — the writers swallow ``OSError`` so a
    full disk cannot break metering — so it cannot be maintained one collection
    at a time. A module that invokes an OS replacement primitive is a second
    owner of the property, whether or not it remembers the cleanup. Temporary
    files alone are not evidence of replacement: response spooling has a
    different owner and lifetime.

    Stated as "nobody here re-implements it" rather than "exactly ``state_file``
    implements it": the owner has since moved out of this package entirely, into
    ``config.atomic_io``, and naming the owner made this guard fail for the one
    change that satisfied it best.
    """

    second_owners: set[str] = set()
    for module in sorted((ROOT / "core/handlers/model_hub").glob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        if any(
            (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
                and node.func.attr in {"rename", "replace"}
            )
            or (
                isinstance(node, ast.ImportFrom)
                and node.module == "os"
                and any(alias.name in {"rename", "replace"} for alias in node.names)
            )
            for node in ast.walk(tree)
        ):
            second_owners.add(module.name)
    assert second_owners == set()
