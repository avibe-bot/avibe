from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_SERVICE_PATH = REPO_ROOT / "storage/sessions_service.py"
ROUTE_OWNED_FIELDS = {"agent_backend", "agent_variant"}


def _save_state_agent_session_upsert_set_keys(tree: ast.AST) -> set[str]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "save_state":
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            if not isinstance(child.func, ast.Attribute) or child.func.attr != "on_conflict_do_update":
                continue
            for keyword in child.keywords:
                if keyword.arg != "set_" or not isinstance(keyword.value, ast.Dict):
                    continue
                keys = {
                    key.value
                    for key in keyword.value.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                }
                if {"scope_id", "session_anchor", "native_session_id"}.issubset(keys):
                    return keys
    return set()


def test_save_state_upsert_set_clause_does_not_relabel_backend_owned_route():
    tree = ast.parse(SESSIONS_SERVICE_PATH.read_text(encoding="utf-8"), filename=str(SESSIONS_SERVICE_PATH))
    keys = _save_state_agent_session_upsert_set_keys(tree)

    assert keys, "save_state upsert set_= clause not found; test needs updating"
    assert keys.isdisjoint(ROUTE_OWNED_FIELDS)
