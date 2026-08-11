from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).parents[1]
TURN_GATEWAY = ROOT / "core/handlers/model_hub/turn_gateway.py"
SERVICE = ROOT / "core/handlers/model_hub/service.py"
PROVENANCE = ROOT / "core/handlers/model_hub/provenance.py"
ROUTER = ROOT / "modules/agents/model_hub.py"
ADAPTER = ROOT / "vibe/model_hub_runtime/adapter.py"


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


def test_g2_outcome_reads_have_one_settlement_owner() -> None:
    # A second ``await handle.outcome()`` is a deliberate structural violation.
    tree = _tree(TURN_GATEWAY)
    owner = _functions(tree)["_settle_consumed_handle"]
    calls = {node.lineno for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "outcome"}
    owned = {node.lineno for node in ast.walk(owner) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "outcome"}
    assert calls == owned


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
