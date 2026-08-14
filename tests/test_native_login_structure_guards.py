from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).parents[1]
SERVICE = ROOT / "core" / "agent_auth_service.py"
NATIVE_ADAPTER = ROOT / "core" / "handlers" / "model_hub" / "native_oauth.py"


def _functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _literal_strings(call: ast.Call) -> set[str]:
    return {
        node.value
        for node in ast.walk(call)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _spawn_sites(tree: ast.Module) -> list[tuple[str, ast.Call]]:
    """Find every provider login operation instead of enumerating today's sites."""
    sites: list[tuple[str, ast.Call]] = []
    for function in _functions(tree):
        for call in ast.walk(function):
            if not isinstance(call, ast.Call):
                continue
            name = _call_name(call)
            literals = _literal_strings(call)
            if name == "create_subprocess_exec" and (
                "--device-auth" in literals or {"auth", "login"} <= literals
            ):
                sites.append((function.name, call))
            elif name in {"_create_claude_control_client", "start_provider_oauth"}:
                sites.append((function.name, call))
    return sites


def test_one_registry_and_one_start_path_are_structural_invariants() -> None:
    tree = ast.parse(SERVICE.read_text(encoding="utf-8"))
    functions = {function.name: function for function in _functions(tree)}
    registry_inits = [
        call
        for call in ast.walk(tree)
        if isinstance(call, ast.Call) and _call_name(call) == "AuthFlowRegistry"
    ]
    assert len(registry_inits) == 1
    assert "_start_auth_flow" in functions

    for public_name in ("start_setup", "start_web_setup"):
        calls = [
            call
            for call in ast.walk(functions[public_name])
            if isinstance(call, ast.Call) and _call_name(call) == "_start_auth_flow"
        ]
        assert calls, f"{public_name} bypasses the shared start path"

    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Attribute) and target.attr in {"_flows", "_web_flows", "_flows_by_id"} for target in node.targets)
    ]
    assert not assignments, "a second flow registry was reintroduced"

    adapter = ast.parse(NATIVE_ADAPTER.read_text(encoding="utf-8"))
    adapter_starts = [
        call
        for function in _functions(adapter)
        for call in ast.walk(function)
        if isinstance(call, ast.Call) and _call_name(call) == "start_web_setup"
    ]
    assert adapter_starts, "Model Hub does not route through the shared start path"


def test_every_discovered_spawn_site_is_reachable_from_shared_start_path() -> None:
    tree = ast.parse(SERVICE.read_text(encoding="utf-8"))
    sites = _spawn_sites(tree)
    assert sites
    start = next(function for function in _functions(tree) if function.name == "_start_auth_flow")
    called_by_start = {
        _call_name(call)
        for call in ast.walk(start)
        if isinstance(call, ast.Call)
    }
    for function_name, _spawn in sites:
        assert function_name == "_start_auth_flow" or function_name in {
            "_start_codex_process",
            "_start_claude_control_flow",
            "_start_opencode_process",
            "_start_opencode_oauth_web",
        }
        assert function_name == "_start_auth_flow" or function_name in called_by_start


def test_model_hub_adapter_has_no_live_flow_registry_or_slot_api() -> None:
    source = NATIVE_ADAPTER.read_text(encoding="utf-8")
    assert "self._flows" not in source
    assert "release_login_slot" not in source
