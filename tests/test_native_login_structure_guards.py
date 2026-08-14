from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).parents[1]
SERVICE = ROOT / "core" / "agent_auth_service.py"
CONTROLLER = ROOT / "core" / "controller.py"
NATIVE_ADAPTER = ROOT / "core" / "handlers" / "model_hub" / "native_oauth.py"
WEB_API = ROOT / "vibe" / "api.py"


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


def _internal_call_graph(
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> dict[str, set[str]]:
    return {
        name: {
            called
            for call in ast.walk(function)
            if isinstance(call, ast.Call)
            if (called := _call_name(call)) in functions
        }
        for name, function in functions.items()
    }


def _reachable(graph: dict[str, set[str]], root: str) -> set[str]:
    reached = {root}
    pending = [root]
    while pending:
        current = pending.pop()
        for called in graph[current] - reached:
            reached.add(called)
            pending.append(called)
    return reached


def test_all_callers_reuse_the_shared_start_path() -> None:
    tree = ast.parse(SERVICE.read_text(encoding="utf-8"))
    functions = {function.name: function for function in _functions(tree)}
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
        and any(
            isinstance(target, ast.Attribute)
            and target.attr in {"_flows", "_web_flows", "_flows_by_id"}
            for target in node.targets
        )
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


def test_web_api_owns_a_separate_service_instance_by_design() -> None:
    """Record the production boundary: Web and controller registries are distinct."""
    web_tree = ast.parse(WEB_API.read_text(encoding="utf-8"))
    web_functions = {function.name: function for function in _functions(web_tree)}
    web_constructors = [
        call
        for call in ast.walk(web_functions["_get_oauth_service"])
        if isinstance(call, ast.Call) and _call_name(call) == "AgentAuthService"
    ]
    controller_tree = ast.parse(CONTROLLER.read_text(encoding="utf-8"))
    controller_constructors = [
        call
        for call in ast.walk(controller_tree)
        if isinstance(call, ast.Call) and _call_name(call) == "AgentAuthService"
    ]
    assert len(web_constructors) == 1
    assert len(controller_constructors) == 1


def test_every_discovered_spawn_site_is_reachable_from_shared_start_path() -> None:
    tree = ast.parse(SERVICE.read_text(encoding="utf-8"))
    functions = {function.name: function for function in _functions(tree)}
    graph = _internal_call_graph(functions)
    sites = _spawn_sites(tree)
    assert sites
    spawn_functions = {function_name for function_name, _spawn in sites}
    guarded_region = _reachable(graph, "_start_auth_flow")
    assert spawn_functions <= guarded_region

    reverse_graph = {name: set() for name in functions}
    for caller, callees in graph.items():
        for callee in callees:
            reverse_graph[callee].add(caller)
    reaches_spawn = set(spawn_functions)
    pending = list(spawn_functions)
    while pending:
        current = pending.pop()
        for caller in reverse_graph[current] - reaches_spawn:
            reaches_spawn.add(caller)
            pending.append(caller)

    # The shared start is the only entry into the internal region that can
    # reach a provider spawn. A new side caller of any helper in that region
    # therefore fails without this test knowing the helper's name.
    for function_name in (reaches_spawn & guarded_region) - {"_start_auth_flow"}:
        assert reverse_graph[function_name] <= guarded_region

    start = functions["_start_auth_flow"]
    claim_lines = [
        call.lineno
        for call in ast.walk(start)
        if isinstance(call, ast.Call) and _call_name(call) == "_claim_shared_native_flow"
    ]
    assert len(claim_lines) == 1
    guarded_entry_lines = [
        call.lineno
        for call in ast.walk(start)
        if isinstance(call, ast.Call)
        and _call_name(call) in reaches_spawn - {"_start_auth_flow"}
    ]
    assert guarded_entry_lines
    assert claim_lines[0] < min(guarded_entry_lines)


def test_model_hub_adapter_has_no_live_flow_registry_or_slot_api() -> None:
    source = NATIVE_ADAPTER.read_text(encoding="utf-8")
    assert "self._flows" not in source
    assert "release_login_slot" not in source
