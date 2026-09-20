from __future__ import annotations

import ast
from pathlib import Path

from modules.agents.catalog import WEB_OAUTH_BACKENDS


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


def _assigned_value_before(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    name: str,
    before_line: int,
) -> ast.AST | None:
    candidates: list[tuple[int, ast.AST]] = []
    for node in ast.walk(function):
        if getattr(node, "lineno", before_line) >= before_line:
            continue
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            candidates.append((node.lineno, node.value))
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value is not None
        ):
            candidates.append((node.lineno, node.value))
    return max(candidates, default=(0, None), key=lambda item: item[0])[1]


def _literal_strings(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    expression: ast.AST,
    *,
    before_line: int | None = None,
    resolving: frozenset[str] = frozenset(),
) -> set[str]:
    """Resolve literals used directly or through a local command variable."""
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        return {expression.value}
    if isinstance(expression, ast.Name) and expression.id not in resolving:
        line = before_line or getattr(expression, "lineno", function.end_lineno or 0)
        assigned = _assigned_value_before(function, expression.id, line)
        if assigned is not None:
            return _literal_strings(
                function,
                assigned,
                before_line=getattr(assigned, "lineno", line),
                resolving=resolving | {expression.id},
            )
    return {
        literal
        for child in ast.iter_child_nodes(expression)
        for literal in _literal_strings(
            function,
            child,
            before_line=before_line,
            resolving=resolving,
        )
    }


def _spawn_sites(tree: ast.Module) -> list[tuple[str, str, ast.Call]]:
    """Find every provider login operation instead of enumerating today's sites."""
    sites: list[tuple[str, str, ast.Call]] = []
    for function in _functions(tree):
        for call in ast.walk(function):
            if not isinstance(call, ast.Call):
                continue
            name = _call_name(call)
            literals = _literal_strings(
                function,
                call,
                before_line=call.lineno,
            )
            is_process_login = name == "create_subprocess_exec" and (
                "--device-auth" in literals or {"auth", "login"} <= literals
            )
            is_control_login = name in {
                "_create_claude_control_client",
                "start_provider_oauth",
            }
            if not (is_process_login or is_control_login):
                continue
            backend_matches = {
                backend
                for backend in WEB_OAUTH_BACKENDS
                if backend in literals or backend in function.name.split("_")
            }
            assert len(backend_matches) == 1, (
                f"cannot associate {function.name}:{call.lineno} with one supported backend"
            )
            sites.append((function.name, backend_matches.pop(), call))
    return sites


def _exception_mentions(handler: ast.ExceptHandler, exception_name: str) -> bool:
    return handler.type is not None and any(
        (isinstance(node, ast.Name) and node.id == exception_name)
        or (isinstance(node, ast.Attribute) and node.attr == exception_name)
        for node in ast.walk(handler.type)
    )


def _caught_code(expression: ast.AST, caught_name: str) -> bool:
    return (
        isinstance(expression, ast.Attribute)
        and expression.attr == "code"
        and isinstance(expression.value, ast.Name)
        and expression.value.id == caught_name
    )


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
    spawn_functions = {function_name for function_name, _backend, _spawn in sites}
    spawn_backends = {backend for _function_name, backend, _spawn in sites}
    assert spawn_backends == set(WEB_OAUTH_BACKENDS)
    backend_start_subprocesses = {
        function.name
        for function in functions.values()
        if function.name.startswith("_start_")
        if set(function.name.split("_")) & set(WEB_OAUTH_BACKENDS)
        if any(
            isinstance(call, ast.Call)
            and _call_name(call) == "create_subprocess_exec"
            for call in ast.walk(function)
        )
    }
    assert backend_start_subprocesses <= spawn_functions
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


def test_native_login_in_progress_identity_reaches_every_payload_unchanged() -> None:
    """Every discovered conflict handler preserves the owner's stable code."""
    production_trees: list[tuple[Path, ast.Module]] = []
    for source_root in (ROOT / "core", ROOT / "vibe"):
        for path in source_root.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if "BackendLoginInProgressError" in source:
                production_trees.append((path, ast.parse(source)))

    definitions = [
        node
        for _path, tree in production_trees
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and node.name == "BackendLoginInProgressError"
    ]
    assert len(definitions) == 1
    codes = {
        value.value
        for definition in definitions
        for node in definition.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id == "code"
        if isinstance((value := node.value), ast.Constant)
        and isinstance(value.value, str)
    }
    assert codes == {"native_login_in_progress"}

    raise_sites = [
        (path, node)
        for path, tree in production_trees
        for node in ast.walk(tree)
        if isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and _call_name(node.exc) == "BackendLoginInProgressError"
    ]
    assert raise_sites

    payload_handlers = 0
    for path, tree in production_trees:
        for handler in (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ExceptHandler)
            and _exception_mentions(node, "BackendLoginInProgressError")
        ):
            literals = {
                node.value
                for node in ast.walk(handler)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            }
            assert "native_source_already_exists" not in literals, path

            caught_name = handler.name
            payload_codes: list[ast.AST] = []
            for node in ast.walk(handler):
                if (
                    isinstance(node, ast.Raise)
                    and isinstance(node.exc, ast.Call)
                    and _call_name(node.exc) == "ModelHubError"
                    and node.exc.args
                ):
                    payload_codes.append(node.exc.args[0])
                elif isinstance(node, ast.Dict):
                    payload_codes.extend(
                        value
                        for key, value in zip(node.keys, node.values)
                        if isinstance(key, ast.Constant) and key.value == "error"
                    )
                elif (
                    isinstance(node, ast.Raise)
                    and node.exc is not None
                    and isinstance(node.exc, ast.Call)
                ):
                    raise AssertionError(
                        f"{path}:{node.lineno} rewraps the native-login conflict"
                    )

            if payload_codes:
                payload_handlers += 1
                assert caught_name is not None
                assert all(_caught_code(code, caught_name) for code in payload_codes), path

    assert payload_handlers >= 2


def test_published_flow_deadline_is_owned_by_its_enforcing_waiter() -> None:
    """One boundary must publish and enforce every auth-flow deadline."""
    tree = ast.parse(SERVICE.read_text(encoding="utf-8"))
    functions = {function.name: function for function in _functions(tree)}

    deadline_writers = {
        function.name
        for function in functions.values()
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        if any(
            isinstance(target, ast.Attribute)
            and target.attr == "expires_at_iso"
            for target in node.targets
        )
    }
    constructor_deadlines = [
        keyword
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        for keyword in call.keywords
        if keyword.arg == "expires_at_iso"
    ]
    assert deadline_writers == {"_arm_flow_waiter"}
    assert not constructor_deadlines

    waiter_writers = {
        function.name
        for function in functions.values()
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        if any(
            isinstance(target, ast.Attribute)
            and target.attr == "waiter_task"
            for target in node.targets
        )
    }
    assert waiter_writers == {"_arm_flow_waiter"}

    armed_waiters = {
        _call_name(waiter_call)
        for call in ast.walk(tree)
        if isinstance(call, ast.Call) and _call_name(call) == "_arm_flow_waiter"
        for argument in call.args[1:]
        for waiter_call in ast.walk(argument)
        if isinstance(waiter_call, ast.Call)
        and (_call_name(waiter_call) or "").startswith("_wait_for_")
    }
    assert armed_waiters
    for waiter_name in armed_waiters:
        assert waiter_name is not None
        timeout_reads = [
            call
            for call in ast.walk(functions[waiter_name])
            if isinstance(call, ast.Call)
            and _call_name(call) == "_remaining_flow_timeout"
        ]
        assert timeout_reads, f"{waiter_name} does not enforce the published deadline"


def test_model_hub_adapter_has_no_live_flow_registry_or_slot_api() -> None:
    source = NATIVE_ADAPTER.read_text(encoding="utf-8")
    assert "self._flows" not in source
    assert "release_login_slot" not in source
