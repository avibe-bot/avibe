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
    ProtocolSSEState,
    ProtocolStreamTaxonomy,
)


ROOT = Path(__file__).parents[1]
TURN_GATEWAY = ROOT / "core/handlers/model_hub/turn_gateway.py"
SERVICE = ROOT / "core/handlers/model_hub/service.py"
PROVENANCE = ROOT / "core/handlers/model_hub/provenance.py"
ROUTER = ROOT / "modules/agents/model_hub.py"
ADAPTER = ROOT / "vibe/model_hub_runtime/adapter.py"
CLIENT = ROOT / "vibe/model_hub_runtime/client.py"

# Owner rulings 19:50-00:06: guard expectations must never derive from guarded code.
# Wire sources:
# - https://platform.claude.com/docs/en/build-with-claude/streaming
# - https://platform.openai.com/docs/api-reference/responses-streaming
# - https://platform.openai.com/docs/api-reference/realtime-server-events/response/done
# - https://platform.openai.com/docs/api-reference/chat/create#chat-create-stream
STREAM_ACCEPTANCE_FIXTURES = {
    "anthropic": {
        "served": (b'{"type":"message_stop"}',),
        "failed_terminal": (
            b'{"type":"error","error":{"type":"permission_error"}}',
        ),
    },
    "openai_responses": {
        "served": (
            b'{"type":"response.completed"}',
            b'{"type":"response.done"}',
        ),
        "failed_terminal": (
            b'{"type":"error","code":"permission_error"}',
            # https://platform.openai.com/docs/api-reference/responses-streaming/response/failed
            b'{"type":"response.failed"}',
            # https://platform.openai.com/docs/api-reference/responses-streaming/response/incomplete
            b'{"type":"response.incomplete"}',
        ),
    },
    "openai_chat": {
        "served": (b"[DONE]",),
        "failed_terminal": (
            b'{"type":"error","error":{"type":"permission_error"}}',
        ),
    },
}
ACCEPTED_SSE_LINE_ENDINGS = (b"\r\n", b"\n", b"\r")
STREAM_BOUNDARY_DIMENSIONS = {
    "transport_event": ("eof", "client_error"),
    "settlement_state": ("pending", "served", "failed_terminal"),
    "line_ending": ACCEPTED_SSE_LINE_ENDINGS,
}
STREAM_BOUNDARY_CASES = tuple(
    (protocol, transport_event, settlement_state, line_ending, payload)
    for protocol, fixtures in STREAM_ACCEPTANCE_FIXTURES.items()
    for transport_event, settlement_state, line_ending in product(
        *STREAM_BOUNDARY_DIMENSIONS.values()
    )
    for payload in (
        (b"{}",)
        if settlement_state == "pending"
        else fixtures[settlement_state]
    )
)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _functions(tree: ast.AST) -> dict[str, ast.AST]:
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


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


def _auth_status_branch(node: ast.AST) -> bool:
    if not isinstance(node, ast.Compare):
        return False
    left = node.left
    is_status = isinstance(left, ast.Name) and left.id in {"status", "http_status"}
    is_status = is_status or (isinstance(left, ast.Attribute) and left.attr == "status")
    return is_status and any(isinstance(value, ast.Constant) and value.value in {401, 403} for value in node.comparators)


def test_g1_cancelled_error_has_one_boundary_owner() -> None:
    # A new ``except asyncio.CancelledError`` outside _handle_request must fail.
    tree = _tree(TURN_GATEWAY)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    found = [_owner_name(node, parents) for node in ast.walk(tree) if isinstance(node, ast.Try) and any(_is_cancel(h) for h in node.handlers)]
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
    calls = {node.lineno for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "outcome"}
    owned = {node.lineno for node in ast.walk(owner) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "outcome"}
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
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr not in {"_settle_turn_handle", "settle_handle_outcome"}:
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


def test_g4_terminal_projection_has_no_execution_channel() -> None:
    # Adding ``supply_channel`` to a projection helper must fail this scan.
    for path in (SERVICE, PROVENANCE, ROUTER):
        for fn in _functions(_tree(path)).values():
            calls_producer = any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "produce_turn_outcome" for node in ast.walk(fn))
            if calls_producer:
                assert not any(isinstance(node, ast.Name) and node.id == "supply_channel" for node in ast.walk(fn))
            if path == ROUTER and getattr(fn, "name", None) == "_no_candidate_error":
                assert any(isinstance(node, ast.Attribute) and node.attr == "_inspect_terminal_chain" for node in ast.walk(fn))


def test_g5_terminalizer_fail_is_validation_only() -> None:
    # A ``terminalizer.fail`` inside cancellation handling must be rejected.
    tree = _tree(TURN_GATEWAY)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "fail":
            assert any(isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name == "_run_request_turn" and fn.lineno <= node.lineno <= fn.end_lineno for fn in ast.walk(tree))


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


def _accepted_types(payloads: tuple[bytes, ...]) -> tuple[frozenset[str], bytes | None]:
    types: set[str] = set()
    literal: bytes | None = None
    for payload in payloads:
        try:
            document = json.loads(payload)
        except ValueError:
            literal = payload
            continue
        assert isinstance(document, dict) and isinstance(document["type"], str)
        types.add(document["type"])
    return frozenset(types), literal


def _assert_stream_taxonomy_matches(
    protocol: str,
    taxonomy: ProtocolStreamTaxonomy,
) -> None:
    fixtures = STREAM_ACCEPTANCE_FIXTURES[protocol]
    success_types, success_literal = _accepted_types(fixtures["served"])
    error_types, _error_literal = _accepted_types(fixtures["failed_terminal"])
    assert taxonomy.success_types == success_types
    assert taxonomy.success_literal == success_literal
    assert taxonomy.error_types == error_types


def test_stream_authority_and_acceptance_fixtures_match_both_ways() -> None:
    assert set(PROTOCOL_STREAM_TAXONOMY) == set(STREAM_ACCEPTANCE_FIXTURES)
    assert set(SSE_LINE_ENDINGS) == set(ACCEPTED_SSE_LINE_ENDINGS)
    for protocol, taxonomy in PROTOCOL_STREAM_TAXONOMY.items():
        _assert_stream_taxonomy_matches(protocol, taxonomy)


def test_stream_authority_guard_rejects_an_unaccepted_alias() -> None:
    taxonomy = PROTOCOL_STREAM_TAXONOMY["openai_responses"]
    mutated = replace(
        taxonomy,
        success_types=taxonomy.success_types | {"response.unaccepted"},
    )
    with pytest.raises(AssertionError):
        _assert_stream_taxonomy_matches("openai_responses", mutated)


def test_sse_tokenizer_bounds_lines_and_frames() -> None:
    with pytest.raises(SSEFrameLimitError, match="line"):
        SSEFrameTokenizer().feed(b"x" * (SSE_MAX_LINE_BYTES + 1))
    tokenizer = SSEFrameTokenizer()
    line = b"x" * SSE_MAX_LINE_BYTES + b"\n"
    for _ in range(SSE_MAX_FRAME_BYTES // SSE_MAX_LINE_BYTES):
        tokenizer.feed(line)
    with pytest.raises(SSEFrameLimitError, match="frame"):
        tokenizer.feed(b"x")


def test_complete_frame_after_terminal_is_always_invalid() -> None:
    frames = (
        b"event: ping\n\n",
        b'data: {"type":"response.output_text.delta"}\n\n',
    )
    for frame in frames:
        state = ProtocolSSEState("openai_responses")
        state.observe(b'data: {"type":"response.completed"}\n\n')
        state.observe(frame)
        assert state.invalid_after_terminal is True


def test_stream_boundary_catalog_exercises_every_enumerated_dimension() -> None:
    seen_states: set[str] = set()
    for protocol, transport_event, settlement_state, line_ending, data in STREAM_BOUNDARY_CASES:
        state = ProtocolSSEState(protocol)
        state.observe(b"data: " + data + line_ending + line_ending)
        if transport_event == "client_error":
            state.observe(b"data: truncated")
        seen_states.add(settlement_state)
        assert state.terminal_outcome == (
            None if settlement_state == "pending" else settlement_state
        )
    assert seen_states == set(STREAM_BOUNDARY_DIMENSIONS["settlement_state"])
