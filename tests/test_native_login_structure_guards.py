from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).parents[1]
SERVICE = ROOT / "core" / "agent_auth_service.py"


def _functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _literal_strings(call: ast.Call) -> list[str]:
    return [node.value for node in ast.walk(call) if isinstance(node, ast.Constant) and isinstance(node.value, str)]


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _native_login_spawn_sites(tree: ast.Module) -> list[tuple[str, ast.Call]]:
    """Discover provider login boundaries from their spawn/API operations."""
    sites: list[tuple[str, ast.Call]] = []
    for function in _functions(tree):
        for call in ast.walk(function):
            if not isinstance(call, ast.Call):
                continue
            name = _call_name(call)
            literals = _literal_strings(call)
            if name == "create_subprocess_exec" and (
                "--device-auth" in literals
                or ("auth" in literals and "login" in literals)
            ):
                sites.append((function.name, call))
            elif name == "_create_claude_control_client":
                sites.append((function.name, call))
            elif name == "start_provider_oauth":
                sites.append((function.name, call))
    return sites


def test_every_native_cli_login_spawn_is_guarded_at_its_owner_boundary() -> None:
    tree = ast.parse(SERVICE.read_text(encoding="utf-8"))
    sites = _native_login_spawn_sites(tree)
    assert sites, "the structure sweep must discover native login spawn sites"

    for function_name, spawn in sites:
        function = next(node for node in _functions(tree) if node.name == function_name)
        claims = [
            call
            for call in ast.walk(function)
            if isinstance(call, ast.Call) and _call_name(call) == "_claim_native_login"
        ]
        assert claims, f"{function_name} reaches a native login spawn without an owner claim"
        assert min(call.lineno for call in claims) < spawn.lineno, (
            f"{function_name} claims after its native login spawn"
        )
        assert any(
            isinstance(node, ast.Try)
            and node.finalbody
            and any(
                isinstance(call, ast.Call)
                and _call_name(call) == "release"
                for statement in node.finalbody
                for call in ast.walk(statement)
            )
            for node in ast.walk(function)
        ), f"{function_name} has no settlement finally that releases its claim"


def test_native_claim_and_release_window_is_synchronous() -> None:
    tree = ast.parse(SERVICE.read_text(encoding="utf-8"))
    functions = _functions(tree)
    for name in ("_claim_native_login", "claim", "release"):
        matches = [function for function in functions if function.name == name]
        assert matches, f"missing synchronous native claim primitive: {name}"
        assert all(isinstance(function, ast.FunctionDef) for function in matches), (
            f"native claim primitive {name} must remain synchronous"
        )
        assert all(not any(isinstance(node, ast.Await) for node in ast.walk(function)) for function in matches), (
            f"native claim primitive {name} must not await"
        )


def test_native_materialization_releases_only_after_durable_write() -> None:
    service = ast.parse(
        (ROOT / "core" / "handlers" / "model_hub" / "service.py").read_text(encoding="utf-8")
    )
    materialize = next(
        function
        for function in _functions(service)
        if function.name == "_materialize_completed_oauth"
    )
    durable_lines = {
        call.lineno
        for call in ast.walk(materialize)
        if isinstance(call, ast.Call)
        and _call_name(call) in {"_materialize_reauth", "_create_oauth_source"}
    }
    release_lines = {
        call.lineno
        for call in ast.walk(materialize)
        if isinstance(call, ast.Call) and _call_name(call) == "release"
    }
    assert durable_lines and release_lines
    assert min(release_lines) > min(durable_lines)


def test_native_claim_release_is_not_timer_owned() -> None:
    tree = ast.parse(SERVICE.read_text(encoding="utf-8"))
    functions = {node.name: node for node in _functions(tree)}
    reap = functions["_reap_stale_web_flows"]
    release_calls = [
        call
        for call in ast.walk(reap)
        if isinstance(call, ast.Call) and _call_name(call) in {"release", "release_login_slot"}
    ]
    assert not release_calls, "terminal-flow TTL cleanup must never release a native credential claim"
