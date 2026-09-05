"""Tests for ``core.internal_server`` — the controller-side Unix socket
ASGI app that exposes ``POST /internal/dispatch_async`` (fire-and-forget turn
dispatch) plus the turn-control surface (cancel / send-now / turn-state) for
the Web UI / CLI callers.

We exercise three layers:

1. The app's request/response shape via ``httpx.ASGITransport`` (no
   actual socket; locks the contract independent of uvicorn).
2. The fire-and-forget dispatch lifecycle: the turn is held open (in_flight)
   and its ``turn.start`` / ``turn.end`` published on the bus, the reply
   itself arriving over ``message.new`` rather than the response.
3. The boot-time socket file lifecycle (default path + chmod).
"""

from __future__ import annotations

import asyncio
import ast
import builtins
import contextlib
import inspect
import socket
import sys
import tempfile
import time
import threading
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, call

import httpx
import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import internal_server, session_turns
from core.controller import Controller
from core.message_context import build_context_turn_sink_key
from core.vibe_agents import VibeAgentStore
from vibe.memory_contract import (
    MemoryImplementationIncompatibleError,
    MemoryImplementationUnavailableError,
    MemoryStoreUnavailableError,
)
from core.run_settlement import SETTLED_BY_STOPPED, SETTLED_BY_TERMINAL_RESULT
from core.services.agent_steering import SteerOutcome, result as steer_result
from core.services.dispatch import (
    SOURCE_HUMAN,
    SOURCE_SCHEDULED,
    TurnDispatchOutcome,
    dispatch_turn,
)
from modules.im import MessageContext
from storage import message_deliveries, resource_access_service
from vibe.authorization import AuthorizationContext
from config.v2_config import MemoryConfig


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _seed_project_workdir(conn, scope_id: str, workdir: Path, *, now: str = "2026-05-31T00:00:00Z") -> None:
    from storage.models import scope_settings

    conn.execute(
        scope_settings.insert().values(
            scope_id=scope_id,
            enabled=1,
            role=None,
            workdir=str(workdir),
            agent_name=None,
            agent_backend=None,
            agent_variant=None,
            model=None,
            reasoning_effort=None,
            require_mention=None,
            settings_version=1,
            settings_json="{}",
            created_at=now,
            updated_at=now,
        )
    )


def _seed_remote_worker(*, backend: str = "claude") -> None:
    store = VibeAgentStore()
    try:
        agent = store.get("worker")
        if agent is None:
            agent = store.create(name="worker", backend=backend)
        with store.engine.begin() as conn:
            resource_access_service.ensure_resource_policy(
                conn,
                resource_kind="agent",
                resource_id=agent.id,
                organization_id="org-1",
                owner_user_id="remote-user",
                owner_email="remote-user@example.com",
                access_level="public",
            )
    finally:
        store.close()


def _authorized_remote_message_metadata() -> dict:
    return resource_access_service.metadata_with_resource_user_context(
        {},
        AuthorizationContext(
            subject="remote-user",
            email="remote-user@example.com",
            instance_role="editor",
            instance_access_source="organization_group",
            organization_id="org-1",
            organization_member_id="member-remote-user",
            organization_role="member",
            group_ids=frozenset({"group-engineering"}),
            claims_issued_at=int(time.time()),
            is_remote=True,
        ),
    )


def _reserve_submission(
    conn,
    *,
    scope_id: str | None,
    session_id: str,
    text: str,
    author: str = "user",
    source: str = "user",
    message_type: str = "user",
    author_name: str | None = None,
    content: dict | None = None,
    metadata: dict | None = None,
    native_message_id: str | None = None,
    message_kind: str | None = None,
):
    delivery_id = message_deliveries.new_delivery_id()
    row = message_deliveries.insert_delivery(
        conn,
        delivery_id=delivery_id,
        session_id=session_id,
        priority="p3",
        state="reserved",
        snapshot=message_deliveries.message_snapshot(
            scope_id=scope_id,
            session_id=session_id,
            platform="avibe",
            author=author,
            source=source,
            message_type=message_type,
            text=text,
            content=content,
            metadata=metadata,
            author_name=author_name,
            native_message_id=native_message_id,
            message_kind=message_kind,
        ),
        dispatch_text=text,
        dedupe_key=(
            f"avibe:{native_message_id}" if native_message_id else None
        ),
        history_event={"kind": "test_admission", "priority": "p3"},
    )
    return message_deliveries.delivery_payload(row)


def _create_test_session(tmp_path: Path, *, native_id: str, backend: str = "claude"):
    from core.services import sessions as sessions_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope

    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id=native_id,
            now="2026-05-31T00:00:00Z",
        )
        _seed_project_workdir(conn, scope_id, tmp_path)
        session = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend=backend,
            agent_name="worker",
        )
    return engine, session


def _create_active_test_turn(
    tmp_path: Path,
    *,
    native_id: str,
    backend: str = "claude",
):
    engine, session = _create_test_session(tmp_path, native_id=native_id, backend=backend)
    with engine.begin() as conn:
        delivery = _reserve_submission(
            conn,
            scope_id=session["scope_id"],
            session_id=session["id"],
            text="active owner",
        )
        turn_id = message_deliveries.new_turn_id()
        message_deliveries.insert_turn(
            conn,
            turn_id=turn_id,
            session_id=session["id"],
            initial_delivery_id=delivery["id"],
            state="starting",
            backend=backend,
        )
        claimed = message_deliveries.open_start_attempt(
            conn,
            delivery["id"],
            expected_version=1,
            turn_id=turn_id,
            attempt_id=message_deliveries.new_attempt_id(),
        )
        assert claimed is not None
        bound = message_deliveries.bind_native_start(
            conn,
            turn_id,
            expected_version=int(message_deliveries.get_turn(conn, turn_id)["version"]),
            runtime_key=f"runtime:{session['id']}",
            runtime_turn_id=f"runtime-turn:{turn_id}",
            native_turn_id=f"native:{turn_id}",
        )
        assert bound is not None
        accepted = message_deliveries.materialize_start_acceptance(
            conn,
            turn_id=turn_id,
            evidence={"kind": "test_native_acceptance"},
        )
        assert accepted
    return engine, session, turn_id


def _bind_test_native_start(engine, context: MessageContext) -> str:
    """Simulate the narrow backend start binding bypassed by dispatch doubles."""

    turn_id = str((context.platform_specific or {}).get("turn_token") or "")
    assert turn_id
    with engine.begin() as conn:
        turn = message_deliveries.get_turn(conn, turn_id)
        assert turn is not None
        if turn["state"] == "starting":
            turn = message_deliveries.bind_native_start(
                conn,
                turn_id,
                expected_version=int(turn["version"]),
                runtime_key=f"runtime:{turn_id}",
                runtime_turn_id=f"runtime-turn:{turn_id}",
                native_turn_id=f"native:{turn_id}",
            )
            assert turn is not None
        delivery = message_deliveries.delivery_for_turn(conn, turn_id)
        assert delivery is not None
        if delivery["state"] != "accepted":
            accepted = message_deliveries.materialize_start_acceptance(
                conn,
                turn_id=turn_id,
                evidence={"kind": "test_native_acceptance"},
            )
            assert accepted
    return turn_id


def _build_controller_double(handler=None):
    """A MagicMock controller whose ``message_handler.handle_user_message``
    can be patched to emit chunks via the real ``_stream_chunk`` hook.

    It carries a *real* turn-sink registry (not MagicMock auto-attrs) so
    ``dispatch_turn`` and ``_stream_chunk`` interoperate exactly as in
    production: dispatch_turn registers the sink, the handler's emits
    resolve it by session key, and a result emit releases the dispatch.
    """

    controller = MagicMock()
    controller.message_handler = MagicMock()

    async def _handle_user_message(
        context,
        text,
        *,
        lifecycle_snapshot=None,
    ):
        payload = context.platform_specific or {}
        assert "_turn_lifecycle_admission" not in payload
        assert "_turn_lifecycle_snapshot" not in payload
        del lifecycle_snapshot
        if handler is not None:
            return await handler(context, text)
        return None

    controller.message_handler.handle_user_message = AsyncMock(
        side_effect=_handle_user_message,
    )

    sinks: dict = {}
    controller.active_turn_sinks = sinks
    controller._get_session_key = lambda ctx: f"{getattr(ctx, 'platform', None)}::{getattr(ctx, 'channel_id', None)}"
    # MUST be set explicitly: a MagicMock would auto-generate this attribute and hand
    # dispatch_turn a bogus key, so every sink lookup would miss and a refused-turn
    # test would hang in ``done.wait()`` instead of failing.
    controller._get_turn_sink_key = lambda ctx: build_context_turn_sink_key(
        ctx, session_key=controller._get_session_key(ctx)
    )

    def _register(session_key, *, on_chunk, done_event, turn_token=None, context=None):
        sinks[session_key] = {"on_chunk": on_chunk, "done_event": done_event, "turn_token": turn_token}

    controller.register_turn_sink = _register

    def _pop(session_key, done_event=None):
        s = sinks.get(session_key)
        if s is None:
            return
        if done_event is not None and s.get("done_event") is not done_event:
            return
        sinks.pop(session_key, None)

    controller.pop_turn_sink = _pop
    controller.get_turn_sink = lambda session_key: sinks.get(session_key)
    controller._session_id_from_context = lambda ctx: str(
        (getattr(ctx, "platform_specific", None) or {}).get("workbench_session_id")
        or (getattr(ctx, "platform_specific", None) or {}).get("agent_session_id")
        or ""
    ) or None

    def _mark_turn_complete(ctx):
        manager = getattr(controller, "session_turns", None)
        if manager is not None:
            spec = getattr(ctx, "platform_specific", None) or {}
            logical_turn_id = str(spec.get("turn_token") or "")
            target = spec.get("agent_session_target") or {}
            backend = str(target.get("agent_backend") or "claude")
            if logical_turn_id:
                manager.on_native_start(
                    ctx,
                    backend=backend,
                    runtime_key=f"runtime:{logical_turn_id}",
                    runtime_turn_id=f"runtime-turn:{logical_turn_id}",
                )
        sink = sinks.get(controller._get_session_key(ctx))
        if sink and sink.get("done_event") is not None:
            sink["done_event"].set()

    controller.mark_turn_complete = _mark_turn_complete

    # Cancel reuses the IM /stop path to interrupt the backend turn.
    controller.command_handler = MagicMock()
    controller.command_handler.handle_stop = AsyncMock(return_value=True)

    # ``_t`` returns the key verbatim so refusal chunks stay JSON-serializable
    # (a bare MagicMock would blow up ``json.dumps`` in ``_sse_event``).
    controller._t = lambda key, **kwargs: key
    controller.config = SimpleNamespace(memory=MemoryConfig(enabled=True))
    controller.memory_adapter = None
    controller.memory_runtime = None
    controller.memory_module = None
    controller._memory_reconcile_task = None
    controller._memory_disabled_cleanup_task = None
    controller._memory_disabled_cleanup_unproved = False
    controller._memory_replacement_gate = None
    controller.default_memory_project_id.return_value = "default"
    for method_name in (
        "_memory_replacement_lock",
        "_await_disabled_memory_cleanup",
        "_attach_memory_runtime",
        "_detach_memory_runtime",
        "_memory_runtime_for_operation",
        "_disabled_memory_source_payload",
        "_disabled_memory_status_payload",
        "_disabled_memory_status_payload_locked",
        "_disabled_memory_processing_record_payload",
        "_disabled_memory_maintenance_payload",
        "_memory_scope_for_runtime",
        "_memory_scope_for_project",
        "wake_memory",
        "install_memory_runtime",
        "memory_status_payload",
        "memory_processing_record_payload",
        "memory_failure_log_payload",
        "memory_maintenance_payload",
        "memory_profile_payload",
        "memory_processing_record_entries_payload",
        "memory_processing_record_entry_payload",
        "memory_projects_payload",
        "memory_search_payload",
        "memory_list_payload",
    ):
        method = getattr(Controller, method_name)
        setattr(controller, method_name, method.__get__(controller, Controller))
    return controller


def test_memory_internal_routes_cannot_bypass_controller_lifecycle() -> None:
    """Wave 0: internal Memory routes must not inspect runtime ownership."""

    tree = ast.parse(inspect.getsource(internal_server.create_app))
    bypasses = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "memory_runtime"
        and isinstance(node.value, ast.Name)
        and node.value.id == "controller"
    ]
    private_runtime_helpers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_memory_runtime"
    ]
    reflective_bypasses = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "controller"
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "memory_runtime"
    ]

    assert bypasses == []
    assert private_runtime_helpers == []
    assert reflective_bypasses == []


def test_memory_internal_server_keeps_implementation_imports_out_of_host_boundary() -> None:
    tree = ast.parse(Path(internal_server.__file__).read_text(encoding="utf-8"))
    implementation_imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "avibe_memory" and [alias.name for alias in node.names] != [
                "CaptureRequest"
            ]:
                implementation_imports.append(node.module)
            elif node.module.startswith("avibe_memory.") and node.module != "core.memory_loader":
                implementation_imports.append(node.module)
        elif isinstance(node, ast.Import):
            implementation_imports.extend(
                alias.name
                for alias in node.names
                if alias.name == "avibe_memory" or alias.name.startswith("avibe_memory.")
            )

    assert implementation_imports == []


def test_disabled_memory_status_route_uses_host_projection_without_runtime() -> None:
    from core.memory_adapter import DisabledMemoryAdapter

    controller = _build_controller_double()
    controller.config.memory = MemoryConfig(enabled=False)
    controller.memory_adapter = DisabledMemoryAdapter()
    controller._create_memory_runtime = Mock(
        side_effect=AssertionError("status must not construct Memory")
    )
    app = internal_server.create_app(controller)

    async def _exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            return await client.get("/internal/memory/status")

    response = asyncio.run(_exercise())

    assert response.status_code == 200
    assert response.json()["state"] == "disabled"
    assert response.json()["source"]["reason"] == "memory_disabled"
    controller._create_memory_runtime.assert_not_called()


def test_memory_status_unknown_failure_uses_stable_envelope() -> None:
    controller = _build_controller_double()
    controller.memory_status_payload = AsyncMock(
        side_effect=RuntimeError("injected lifecycle failure")
    )
    app = internal_server.create_app(controller)

    async def _exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            return await client.get("/internal/memory/status")

    response = asyncio.run(_exercise())

    assert response.status_code == 503
    assert response.json() == {"error": "memory_store_unavailable"}


def test_memory_projects_unknown_lifecycle_failure_uses_stable_envelope() -> None:
    from vibe.memory_http_headers import CALLER_SESSION_HEADER

    controller = _build_controller_double()
    controller.memory_scope_for_cli_session.return_value = (
        "u-11111111111111111111111111111111",
        "default",
    )
    controller.memory_projects_payload = AsyncMock(
        side_effect=RuntimeError("injected lifecycle failure")
    )
    app = internal_server.create_app(controller)

    async def _exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            return await client.get(
                "/internal/memory/projects",
                headers={CALLER_SESSION_HEADER: "session-1"},
            )

    response = asyncio.run(_exercise())

    assert response.status_code == 503
    assert response.json() == {
        "status": "failed",
        "error": "memory_store_unavailable",
    }


@pytest.mark.parametrize(
    ("error_type", "expected_code"),
    [
        ("MemoryImplementationUnavailableError", "memory_implementation_unavailable"),
        ("MemoryImplementationIncompatibleError", "memory_implementation_incompatible"),
    ],
)
def test_memory_projects_implementation_failure_uses_stable_error_envelope(
    error_type: str,
    expected_code: str,
) -> None:
    from core import internal_server as server
    from vibe.memory_http_headers import CALLER_SESSION_HEADER
    from vibe.memory_contract import (
        MemoryImplementationIncompatibleError,
        MemoryImplementationUnavailableError,
    )

    error_class = {
        "MemoryImplementationUnavailableError": MemoryImplementationUnavailableError,
        "MemoryImplementationIncompatibleError": MemoryImplementationIncompatibleError,
    }[error_type]
    controller = _build_controller_double()
    controller.memory_scope_for_cli_session.return_value = (
        "u-11111111111111111111111111111111",
        "default",
    )
    controller.memory_projects_payload = AsyncMock(side_effect=error_class("injected"))
    app = server.create_app(controller)

    async def _exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(
                "/internal/memory/projects",
                headers={CALLER_SESSION_HEADER: "session-1"},
            )

    response = asyncio.run(_exercise())

    assert response.status_code == 503
    assert response.json() == {"status": "failed", "error": expected_code}


def test_memory_projects_implementation_failure_preserves_admitted_cli_session_boundary() -> None:
    from core import internal_server as server
    from vibe.memory_http_headers import CALLER_SESSION_HEADER
    from vibe.memory_contract import MemoryImplementationUnavailableError

    controller = Controller.__new__(Controller)
    controller.config = SimpleNamespace(memory=SimpleNamespace(enabled=True))
    controller._memory_implementation_error = MemoryImplementationUnavailableError("injected")
    controller._memory_scopes_by_session = {}
    controller._memory_cli_facts_by_session = {}
    controller._memory_implementation_cli_sessions = set()
    controller._memory_admission = lambda: pytest.fail(
        "implementation failure must be projected before admission imports"
    )
    controller._memory_turn_facts = lambda _context: pytest.fail(
        "implementation failure must be projected before facts imports"
    )
    controller.memory_projects_payload = AsyncMock(
        side_effect=MemoryImplementationUnavailableError("injected")
    )

    context = SimpleNamespace(
        platform_specific={
            "agent_session_target": {"id": "session-1"},
        },
        platform="avibe",
    )
    assert Controller.configure_memory_cli_session(
        controller,
        context,
        admitted=True,
    )
    assert controller._memory_implementation_cli_sessions == {"session-1"}
    app = server.create_app(controller)

    async def _exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(
                "/internal/memory/projects",
                headers={CALLER_SESSION_HEADER: "session-1"},
            )

    response = asyncio.run(_exercise())

    assert response.status_code == 503
    assert response.json() == {"status": "failed", "error": "memory_implementation_unavailable"}


@pytest.mark.parametrize(
    ("implementation_error", "expected_error"),
    [
        (MemoryImplementationUnavailableError("injected"), "memory_implementation_unavailable"),
        (MemoryImplementationIncompatibleError("injected"), "memory_implementation_incompatible"),
    ],
)
def test_memory_remember_implementation_failure_uses_stable_error_envelope(
    monkeypatch: pytest.MonkeyPatch,
    implementation_error: BaseException,
    expected_error: str,
) -> None:
    from vibe.memory_http_headers import CALLER_SESSION_HEADER

    controller = Controller.__new__(Controller)
    controller.config = SimpleNamespace(memory=SimpleNamespace(enabled=True))
    controller._memory_implementation_error = implementation_error
    controller.memory_scope_for_cli_session = lambda _session_id: (
        "u-11111111111111111111111111111111",
        "default",
    )
    controller.capture_memory = Mock(
        side_effect=AssertionError(
            "implementation failure must short-circuit before capture"
        )
    )
    real_import = builtins.__import__

    def fail_memory_type_import(name, *args, **kwargs):
        if name == "avibe_memory" or name.startswith("avibe_memory."):
            raise RuntimeError("optional implementation import failed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_memory_type_import)
    app = internal_server.create_app(controller)

    async def _exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/internal/memory/remember",
                headers={CALLER_SESSION_HEADER: "session-1"},
                json={"text": "remember this"},
            )

    response = asyncio.run(_exercise())

    assert response.status_code == 503
    assert response.json() == {"status": "failed", "error": expected_error}


def test_memory_install_route_delegates_to_controller_lifecycle() -> None:
    controller = _build_controller_double()
    controller.memory_runtime = None
    controller.install_memory_runtime = AsyncMock(return_value={"ok": True})
    app = internal_server.create_app(controller)

    async def _exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            return await client.post("/internal/memory/install-runtime")

    response = asyncio.run(_exercise())

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    controller.install_memory_runtime.assert_awaited_once_with()


def test_controller_double_omits_retired_turn_lifecycle_admission() -> None:
    async def _exercise() -> None:
        context = MessageContext(
            user_id="U",
            channel_id="C",
            platform="avibe",
            platform_specific={},
        )

        async def handler(received_context, _text):
            assert "_turn_lifecycle_admission" not in (
                received_context.platform_specific or {}
            )

        controller = _build_controller_double(handler=handler)
        await controller.message_handler.handle_user_message(context, "hello")

    asyncio.run(_exercise())


def test_running_agents_snapshot_bounds_ownership_candidates(monkeypatch) -> None:
    controller = _build_controller_double()
    captured = []

    def _snapshot(_controller, *, ownership_candidate_run_ids=None):
        captured.append(ownership_candidate_run_ids)
        return {"ok": True, "agents": [], "owned_run_ids": []}

    monkeypatch.setattr("core.services.running_agents.snapshot_running_agents", _snapshot)
    app = internal_server.create_app(controller)

    async def _exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            valid = await client.post(
                "/internal/running-agents/snapshot",
                json={"run_ids": ["run-a", "run-b"]},
            )
            oversized = await client.post(
                "/internal/running-agents/snapshot",
                json={"run_ids": [f"run-{index}" for index in range(102)]},
            )
        return valid, oversized

    valid, oversized = asyncio.run(_exercise())

    assert valid.status_code == 200
    assert oversized.status_code == 400
    assert oversized.json()["error"] == "invalid_run_candidates"
    assert captured == [["run-a", "run-b"]]


def test_memory_archive_session_delegates_raw_identity_with_bounded_lifecycle() -> None:
    controller = _build_controller_double()
    controller.memory_scope_for_cli_session = Mock(
        side_effect=AssertionError("the endpoint must not resolve identity")
    )
    controller.archive_session = AsyncMock(
        return_value={"id": "ses-memory", "status": "archived"}
    )
    app = internal_server.create_app(controller)

    async def _exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            return await client.post(
                "/internal/memory/archive-session",
                json={"session_id": "ses-memory"},
            )

    response = asyncio.run(_exercise())

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "session": {"id": "ses-memory", "status": "archived"},
    }
    # The UI transport waits without a reporting deadline so the controller can
    # finish the terminal archive write. Memory flush is scheduled afterwards.
    controller.archive_session.assert_awaited_once_with(
        "ses-memory",
        deadline_seconds=5.0,
    )
    controller.memory_scope_for_cli_session.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"session_id": "   "},
        {"session_id": " ses-memory"},
        {"session_id": 123},
        {"session_id": "ses-memory", "principal_id": "u-untrusted"},
    ],
)
def test_memory_archive_session_rejects_widened_or_invalid_payloads(
    payload: dict[str, object],
) -> None:
    controller = _build_controller_double()
    controller.archive_session = AsyncMock(return_value={})
    app = internal_server.create_app(controller)

    async def _exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            return await client.post(
                "/internal/memory/archive-session",
                json=payload,
            )

    response = asyncio.run(_exercise())

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "memory_invalid_input"}
    controller.archive_session.assert_not_awaited()


@pytest.mark.parametrize(
    "error,status_code,error_code",
    [
        (LookupError("missing"), 404, "session_not_found"),
        (RuntimeError("failed"), 503, "session_archive_unavailable"),
    ],
)
def test_memory_archive_session_returns_closed_failure_codes(
    error: Exception,
    status_code: int,
    error_code: str,
) -> None:
    controller = _build_controller_double()
    controller.archive_session = AsyncMock(side_effect=error)
    app = internal_server.create_app(controller)

    async def _exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            return await client.post(
                "/internal/memory/archive-session",
                json={"session_id": "ses-memory"},
            )

    response = asyncio.run(_exercise())

    assert response.status_code == status_code
    assert response.json() == {"ok": False, "error": error_code}


def test_memory_recovery_reads_resolve_only_signed_ui_operators() -> None:
    from vibe.memory_http_headers import MEMORY_USER_KEY_HEADER
    from vibe.memory_ui_access import MEMORY_UI_PROOF_HEADER, build_ui_read_proof

    secret = "test-memory-ui-secret"
    calls: list[tuple[str, str | None]] = []

    class Runtime:
        def principal_for_user_key(self, user_key: str) -> str:
            return {
                "avibe:local": "u-local-principal",
                "avibe:remote:subject-2": "u-remote-principal",
            }[user_key]

        async def processing_record_payload(
            self,
            *,
            verified_user_key: str | None = None,
        ):
            calls.append(("processing-record", verified_user_key))
            return {
                "status": "ok",
                "runtime": {"source": {"status": "unavailable"}, "health": None},
                "sources": {},
                "anomalies": {"source": {"status": "available"}, "items": []},
                "maintenance": {"source": {"status": "available"}},
            }

        async def failure_log_payload(
            self,
            *,
            verified_user_key: str | None = None,
        ):
            calls.append(("failures", verified_user_key))
            return {"status": "ok", "items": [], "recovery": None}

        async def maintenance_payload(
            self,
            *,
            verified_user_key: str | None = None,
        ):
            calls.append(("maintenance", verified_user_key))
            return {
                "status": "ok",
                "data_exists": False,
                "can_clear": True,
                "clear_in_progress": None,
            }

    controller = _build_controller_double()
    controller.memory_runtime = Runtime()
    app = internal_server.create_app(controller, memory_ui_secret=secret)

    def headers(path: str, user_key: str) -> dict[str, str]:
        return {
            MEMORY_USER_KEY_HEADER: user_key,
            MEMORY_UI_PROOF_HEADER: build_ui_read_proof(
                secret,
                method="GET",
                path=path,
                user_key=user_key,
            ),
        }

    async def _exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            composite = await client.get(
                "/internal/memory/processing-record",
                headers=headers(
                    "/internal/memory/processing-record",
                    "avibe:remote:subject-2",
                ),
            )
            local = await client.get(
                "/internal/memory/failures",
                headers=headers("/internal/memory/failures", "avibe:local"),
            )
            remote = await client.get(
                "/internal/memory/maintenance",
                headers=headers(
                    "/internal/memory/maintenance",
                    "avibe:remote:subject-2",
                ),
            )
            unsigned = await client.get("/internal/memory/failures")
            return composite, local, remote, unsigned

    composite, local, remote, unsigned = asyncio.run(_exercise())

    assert composite.status_code == local.status_code == remote.status_code == unsigned.status_code == 200
    assert "avibe:remote:subject-2" not in composite.text
    assert "u-remote-principal" not in composite.text
    assert calls == [
        ("processing-record", "avibe:remote:subject-2"),
        ("failures", "avibe:local"),
        ("maintenance", "avibe:remote:subject-2"),
        ("failures", None),
    ]


def test_memory_search_accepts_bounded_agentic_policy_from_cli_session() -> None:
    from vibe.memory_http_headers import CALLER_SESSION_HEADER

    captured: list[tuple] = []

    class Runtime:
        async def search_payload(
            self,
            query,
            policy,
            principal_id,
            project_id,
            *,
            current_session_id=None,
        ):
            captured.append(
                (query, policy, principal_id, project_id, current_session_id)
            )
            return {"status": "ok", "items": []}

    controller = _build_controller_double()
    controller.memory_scope_for_cli_session.return_value = (
        "u-11111111111111111111111111111111",
        "default",
    )
    controller.memory_runtime = Runtime()
    app = internal_server.create_app(controller)

    async def _exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/internal/memory/search",
                headers={CALLER_SESSION_HEADER: "ses-memory"},
                json={
                    "query": "connect the clues",
                    "policy": {
                        "mode": "agentic",
                        "max_results": 8,
                        "include_profile": True,
                        "include_current_session": False,
                        "timeout_seconds": 30,
                        "max_model_calls": 2,
                        "cost_budget_tokens": 32_000,
                    },
                },
            )

    response = asyncio.run(_exercise())

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "items": []}
    assert len(captured) == 1
    query, policy, principal_id, project_id, current_session_id = captured[0]
    assert query == "connect the clues"
    assert policy.mode == "agentic"
    assert policy.timeout_seconds == 30
    assert principal_id == "u-11111111111111111111111111111111"
    assert project_id == "default"
    assert current_session_id == "ses-memory"


def test_memory_search_route_does_not_import_memory_types_on_request(monkeypatch) -> None:
    import builtins

    from vibe.memory_http_headers import CALLER_SESSION_HEADER

    controller = _build_controller_double()
    controller.memory_scope_for_cli_session.return_value = (
        "u-11111111111111111111111111111111",
        "default",
    )
    controller.memory_search_payload = AsyncMock(
        return_value={"status": "ok", "items": []}
    )
    app = internal_server.create_app(controller)
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "avibe_memory.types":
            raise RuntimeError("optional implementation initializer")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    async def _exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/internal/memory/search",
                headers={CALLER_SESSION_HEADER: "ses-memory"},
                json={"query": "connect the clues", "policy": {"mode": "keyword"}},
            )

    response = asyncio.run(_exercise())

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "items": []}


def test_memory_list_accepts_everos_maximum_at_controller_boundary() -> None:
    """MEMORY-LIST-009: the internal socket accepts the EverOS maximum."""

    from vibe.memory_http_headers import CALLER_SESSION_HEADER

    runtime = SimpleNamespace(
        list_memory_projects=AsyncMock(return_value=("default", "notes")),
        list_episodes_payload=AsyncMock(
            return_value={"status": "ok", "items": [], "page": 2}
        ),
    )
    controller = _build_controller_double()
    controller.memory_scope_for_cli_session.return_value = (
        "u-11111111111111111111111111111111",
        "default",
    )
    controller.memory_runtime = runtime
    app = internal_server.create_app(controller)

    async def _exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/internal/memory/list",
                headers={CALLER_SESSION_HEADER: "ses-memory-list"},
                json={"project": "notes", "page": 2, "limit": 100},
            )

    response = asyncio.run(_exercise())

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "items": [], "page": 2}
    controller.memory_scope_for_cli_session.assert_called_once_with("ses-memory-list")
    runtime.list_memory_projects.assert_awaited_once_with(
        "u-11111111111111111111111111111111"
    )
    runtime.list_episodes_payload.assert_awaited_once_with(
        "u-11111111111111111111111111111111",
        "notes",
        page=2,
        page_size=100,
    )


def test_memory_list_reports_unavailable_store_before_named_project_validation() -> None:
    from vibe.memory_http_headers import CALLER_SESSION_HEADER

    runtime = SimpleNamespace(
        available=False,
        list_memory_projects=AsyncMock(return_value=("default",)),
        list_episodes_payload=AsyncMock(),
    )
    controller = _build_controller_double()
    controller.memory_scope_for_cli_session.return_value = (
        "u-11111111111111111111111111111111",
        "default",
    )
    controller.memory_runtime = runtime
    app = internal_server.create_app(controller)

    async def _exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/internal/memory/list",
                headers={CALLER_SESSION_HEADER: "ses-memory-list"},
                json={"project": "notes", "page": 1, "limit": 20},
            )

    response = asyncio.run(_exercise())

    assert response.status_code == 503
    assert response.json() == {
        "status": "failed",
        "error": "memory_store_unavailable",
    }
    runtime.list_memory_projects.assert_not_awaited()
    runtime.list_episodes_payload.assert_not_awaited()


def test_memory_list_rejects_all_for_cli_at_controller_boundary() -> None:
    from vibe.memory_http_headers import CALLER_SESSION_HEADER

    runtime = SimpleNamespace(
        list_all_episodes_payload=AsyncMock(),
        list_episodes_payload=AsyncMock(),
    )
    controller = _build_controller_double()
    controller.memory_scope_for_cli_session.return_value = (
        "u-11111111111111111111111111111111",
        "default",
    )
    controller.memory_runtime = runtime
    app = internal_server.create_app(controller)

    async def _exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/internal/memory/list",
                headers={CALLER_SESSION_HEADER: "ses-memory-list"},
                json={"project": "all", "page": 1, "limit": 20},
            )

    response = asyncio.run(_exercise())

    assert response.status_code == 400
    assert response.json() == {
        "status": "failed",
        "error": "memory_invalid_input",
    }
    runtime.list_all_episodes_payload.assert_not_awaited()
    runtime.list_episodes_payload.assert_not_awaited()


def test_memory_list_all_is_available_only_to_signed_ui_principal() -> None:
    """MEMORY-LIST-003, MEMORY-LIST-008."""
    from vibe.memory_http_headers import MEMORY_USER_KEY_HEADER
    from vibe.memory_ui_access import MEMORY_UI_PROOF_HEADER, build_ui_read_proof

    secret = "test-memory-ui-secret"
    runtime = SimpleNamespace(
        resolve_principal_for_user_key=AsyncMock(
            return_value="u-22222222222222222222222222222222"
        ),
        list_all_episodes_payload=AsyncMock(
            return_value={
                "status": "ok",
                "items": [],
                "next_cursor": "next-token",
            }
        ),
    )
    controller = _build_controller_double()
    controller.default_memory_project_id.return_value = "default"
    controller.memory_runtime = runtime
    app = internal_server.create_app(controller, memory_ui_secret=secret)
    user_key = "avibe:remote:subject-list"

    async def _exercise() -> httpx.Response:
        path = "/internal/memory/list"
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                path,
                headers={
                    MEMORY_USER_KEY_HEADER: user_key,
                    MEMORY_UI_PROOF_HEADER: build_ui_read_proof(
                        secret,
                        method="POST",
                        path=path,
                        user_key=user_key,
                    ),
                },
                json={
                    "project": "all",
                    "cursor": "cursor-token",
                    "limit": 7,
                    "origin": "agent",
                },
            )

    response = asyncio.run(_exercise())

    assert response.status_code == 200
    assert response.json()["next_cursor"] == "next-token"
    runtime.resolve_principal_for_user_key.assert_awaited_once_with(user_key)
    runtime.list_all_episodes_payload.assert_awaited_once_with(
        "u-22222222222222222222222222222222",
        cursor="cursor-token",
        limit=7,
        origin="agent",
    )


def test_memory_list_rejects_agent_origin_for_cli_callers() -> None:
    from vibe.memory_http_headers import CALLER_SESSION_HEADER

    runtime = SimpleNamespace(list_episodes_payload=AsyncMock())
    controller = _build_controller_double()
    controller.memory_scope_for_cli_session.return_value = (
        "u-11111111111111111111111111111111",
        "default",
    )
    controller.memory_runtime = runtime
    app = internal_server.create_app(controller)

    async def _exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/internal/memory/list",
                headers={CALLER_SESSION_HEADER: "ses-memory-list"},
                json={
                    "project": "default",
                    "page": 1,
                    "limit": 20,
                    "origin": "agent",
                },
            )

    response = asyncio.run(_exercise())

    assert response.status_code == 400
    assert response.json() == {
        "status": "failed",
        "error": "memory_invalid_input",
    }
    runtime.list_episodes_payload.assert_not_awaited()


def test_memory_list_rejects_invalid_aggregate_cursor_at_controller_boundary() -> None:
    from vibe.memory_http_headers import MEMORY_USER_KEY_HEADER
    from vibe.memory_ui_access import MEMORY_UI_PROOF_HEADER, build_ui_read_proof

    secret = "test-memory-ui-secret"
    runtime = SimpleNamespace(
        resolve_principal_for_user_key=AsyncMock(
            return_value="u-22222222222222222222222222222222"
        ),
        list_all_episodes_payload=AsyncMock(
            return_value={"status": "failed", "error": "memory_invalid_input"}
        ),
    )
    controller = _build_controller_double()
    controller.default_memory_project_id.return_value = "default"
    controller.memory_runtime = runtime
    app = internal_server.create_app(controller, memory_ui_secret=secret)
    path = "/internal/memory/list"
    user_key = "avibe:local"

    async def _exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                path,
                headers={
                    MEMORY_USER_KEY_HEADER: user_key,
                    MEMORY_UI_PROOF_HEADER: build_ui_read_proof(
                        secret,
                        method="POST",
                        path=path,
                        user_key=user_key,
                    ),
                },
                json={"project": "all", "cursor": "malformed", "limit": 20},
            )

    response = asyncio.run(_exercise())

    assert response.status_code == 400
    assert response.json() == {
        "status": "failed",
        "error": "memory_invalid_input",
    }


def test_memory_list_rejects_surrogate_aggregate_cursor_at_controller_boundary() -> None:
    from vibe.memory_http_headers import MEMORY_USER_KEY_HEADER
    from vibe.memory_ui_access import MEMORY_UI_PROOF_HEADER, build_ui_read_proof

    secret = "test-memory-ui-secret"
    runtime = SimpleNamespace(
        resolve_principal_for_user_key=AsyncMock(
            return_value="u-22222222222222222222222222222222"
        ),
        list_all_episodes_payload=AsyncMock(),
    )
    controller = _build_controller_double()
    controller.default_memory_project_id.return_value = "default"
    controller.memory_runtime = runtime
    app = internal_server.create_app(controller, memory_ui_secret=secret)
    path = "/internal/memory/list"
    user_key = "avibe:local"

    async def _exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                path,
                headers={
                    "content-type": "application/json",
                    MEMORY_USER_KEY_HEADER: user_key,
                    MEMORY_UI_PROOF_HEADER: build_ui_read_proof(
                        secret,
                        method="POST",
                        path=path,
                        user_key=user_key,
                    ),
                },
                content='{"project":"all","cursor":"\\ud800","limit":20}',
            )

    response = asyncio.run(_exercise())

    assert response.status_code == 400
    assert response.json() == {
        "status": "failed",
        "error": "memory_invalid_input",
    }
    runtime.list_all_episodes_payload.assert_not_awaited()


def test_memory_list_accepts_maximum_aggregate_cursor_transport_bound(monkeypatch) -> None:
    import builtins

    from vibe.memory_http_headers import MEMORY_USER_KEY_HEADER
    from core.memory_loader import MEMORY_LIST_CURSOR_MAX_BYTES
    from vibe.memory_ui_access import MEMORY_UI_PROOF_HEADER, build_ui_read_proof

    secret = "test-memory-ui-secret"
    cursor = "a" * MEMORY_LIST_CURSOR_MAX_BYTES
    runtime = SimpleNamespace(
        resolve_principal_for_user_key=AsyncMock(
            return_value="u-22222222222222222222222222222222"
        ),
        list_all_episodes_payload=AsyncMock(
            return_value={"status": "ok", "items": [], "next_cursor": None}
        ),
    )
    controller = _build_controller_double()
    controller.default_memory_project_id.return_value = "default"
    controller.memory_runtime = runtime
    app = internal_server.create_app(controller, memory_ui_secret=secret)
    path = "/internal/memory/list"
    user_key = "avibe:local"
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "avibe_memory.runtime":
            raise RuntimeError("optional implementation initializer")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    async def _exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            return await client.post(
                path,
                headers={
                    MEMORY_USER_KEY_HEADER: user_key,
                    MEMORY_UI_PROOF_HEADER: build_ui_read_proof(
                        secret,
                        method="POST",
                        path=path,
                        user_key=user_key,
                    ),
                },
                json={"project": "all", "cursor": cursor, "limit": 20},
            )

    response = asyncio.run(_exercise())

    assert response.status_code == 200
    runtime.list_all_episodes_payload.assert_awaited_once_with(
        "u-22222222222222222222222222222222",
        cursor=cursor,
        limit=20,
    )


def test_memory_wake_uses_non_destructive_runtime_operation() -> None:
    runtime = SimpleNamespace(
        wake=AsyncMock(return_value={"ok": True, "state": "running"})
    )
    controller = _build_controller_double()
    controller.memory_runtime = runtime
    app = internal_server.create_app(controller, memory_ui_secret="test-secret")

    async def _exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/internal/memory/wake", json={})

    response = asyncio.run(_exercise())
    assert response.status_code == 200
    assert response.json() == {"ok": True, "state": "running"}
    runtime.wake.assert_awaited_once_with()


def test_memory_wake_preserves_store_unavailable_outcome() -> None:
    controller = _build_controller_double()
    controller.wake_memory = AsyncMock(
        side_effect=MemoryStoreUnavailableError(
            "Disabled Memory cleanup is still in progress"
        )
    )
    app = internal_server.create_app(controller, memory_ui_secret="test-secret")

    async def _exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            return await client.post("/internal/memory/wake", json={})

    response = asyncio.run(_exercise())

    assert response.status_code == 503
    assert response.json() == {
        "ok": False,
        "state": "degraded",
        "error": "memory_store_unavailable",
    }
    controller.wake_memory.assert_awaited_once_with()


def test_reconcile_memory_hot_applies_the_persisted_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from config.v2_config import MemoryConfig, V2Config

    memory = MemoryConfig(enabled=False)
    monkeypatch.setattr(
        V2Config,
        "load",
        classmethod(lambda cls: SimpleNamespace(memory=memory)),
    )
    controller = _build_controller_double()
    controller.reconcile_memory = AsyncMock(
        return_value={"ok": True, "state": "disabled"}
    )
    app = internal_server.create_app(controller, memory_ui_secret="test-secret")

    async def _exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            return await client.post("/internal/reconcile-memory", json={})

    response = asyncio.run(_exercise())

    assert response.status_code == 200
    assert response.json() == {"ok": True, "state": "disabled"}
    controller.reconcile_memory.assert_awaited_once_with(memory)


def test_memory_preflight_requires_signed_ui_operator() -> None:
    controller = _build_controller_double()
    controller.preflight_memory = AsyncMock()
    app = internal_server.create_app(controller, memory_ui_secret="test-secret")

    async def _exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/internal/memory/preflight", json={"memory": {}})

    response = asyncio.run(_exercise())
    assert response.status_code == 403
    assert response.json() == {"ok": False, "error": "memory_access_denied"}
    controller.preflight_memory.assert_not_awaited()


def test_memory_preflight_uses_controller_lifecycle_when_runtime_is_disabled() -> None:
    from config.v2_config import (
        MemoryConfig,
        MemoryEndpointConfig,
        MemoryProcessingConfig,
        memory_config_to_payload,
    )
    from vibe.memory_http_headers import MEMORY_USER_KEY_HEADER
    from vibe.memory_ui_access import MEMORY_UI_PROOF_HEADER, build_ui_read_proof

    secret = "test-memory-ui-secret"
    path = "/internal/memory/preflight"
    candidate = MemoryConfig(
        enabled=True,
        processing=MemoryProcessingConfig(
            llm=MemoryEndpointConfig(
                base_url="https://llm.example/v1",
                model="chat-model",
                api_key="llm-secret",
            ),
            embedding=MemoryEndpointConfig(
                base_url="https://embedding.example/v1",
                model="embedding-model",
                api_key="embedding-secret",
            ),
        ),
    )
    controller = _build_controller_double()
    controller.memory_runtime = None
    controller.preflight_memory = AsyncMock(return_value={"ok": True})
    app = internal_server.create_app(controller, memory_ui_secret=secret)
    headers = {
        MEMORY_USER_KEY_HEADER: "avibe:local",
        MEMORY_UI_PROOF_HEADER: build_ui_read_proof(
            secret,
            method="POST",
            path=path,
            user_key="avibe:local",
        ),
    }

    async def _exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                path,
                headers=headers,
                json={"memory": memory_config_to_payload(candidate, include_secrets=True)},
            )

    response = asyncio.run(_exercise())

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    controller.preflight_memory.assert_awaited_once_with(candidate)


@pytest.mark.parametrize(
    ("path", "method_name", "operation"),
    [
        ("/internal/memory/repair", "repair_memory", "repair"),
        ("/internal/memory/delete-data", "delete_memory_data", "delete_data"),
    ],
)
def test_memory_data_operations_require_signed_ui_operator(
    path: str,
    method_name: str,
    operation: str,
) -> None:
    from vibe.memory_http_headers import MEMORY_USER_KEY_HEADER
    from vibe.memory_ui_access import MEMORY_UI_PROOF_HEADER

    secret = "test-memory-ui-secret"
    controller = _build_controller_double()
    handler = AsyncMock()
    setattr(controller, method_name, handler)
    app = internal_server.create_app(controller, memory_ui_secret=secret)

    async def _exercise() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            unsigned = await client.post(
                path,
                json={"confirm_loss": True},
            )
            invalid = await client.post(
                path,
                json={"confirm_loss": True},
                headers={
                    MEMORY_USER_KEY_HEADER: "avibe:local",
                    MEMORY_UI_PROOF_HEADER: "invalid-proof",
                },
            )
        return unsigned, invalid

    unsigned, invalid = asyncio.run(_exercise())
    assert unsigned.status_code == invalid.status_code == 403
    assert unsigned.json() == invalid.json() == {
        "ok": False,
        "operation": operation,
        "error": "memory_access_denied",
    }
    handler.assert_not_awaited()


@pytest.mark.parametrize(
    ("path", "method_name", "operation"),
    [
        ("/internal/memory/repair", "repair_memory", "repair"),
        ("/internal/memory/delete-data", "delete_memory_data", "delete_data"),
    ],
)
def test_memory_data_operations_require_exact_loss_confirmation(
    path: str,
    method_name: str,
    operation: str,
) -> None:
    from vibe.memory_http_headers import MEMORY_USER_KEY_HEADER
    from vibe.memory_ui_access import MEMORY_UI_PROOF_HEADER, build_ui_read_proof

    secret = "test-memory-ui-secret"
    controller = _build_controller_double()
    handler = AsyncMock()
    setattr(controller, method_name, handler)
    app = internal_server.create_app(controller, memory_ui_secret=secret)
    headers = {
        MEMORY_USER_KEY_HEADER: "avibe:local",
        MEMORY_UI_PROOF_HEADER: build_ui_read_proof(
            secret,
            method="POST",
            path=path,
            user_key="avibe:local",
        ),
    }

    async def _exercise() -> list[httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return [
                await client.post(path, json=payload, headers=headers)
                for payload in (
                    {},
                    {"confirm_loss": False},
                    {"confirm": True},
                    {"confirm_loss": True, "extra": True},
                )
            ]

    responses = asyncio.run(_exercise())
    for response in responses:
        assert response.status_code == 400
        assert response.json() == {
            "ok": False,
            "operation": operation,
            "error": "memory_loss_confirmation_required",
            "result": "unchanged",
        }
    handler.assert_not_awaited()


@pytest.mark.parametrize(
    ("path", "method_name", "operation"),
    [
        ("/internal/memory/repair", "repair_memory", "repair"),
        ("/internal/memory/delete-data", "delete_memory_data", "delete_data"),
    ],
)
def test_memory_data_operations_return_distinct_final_result(
    path: str,
    method_name: str,
    operation: str,
) -> None:
    from vibe.memory_http_headers import MEMORY_USER_KEY_HEADER
    from vibe.memory_ui_access import MEMORY_UI_PROOF_HEADER, build_ui_read_proof

    secret = "test-memory-ui-secret"
    result = {
        "ok": True,
        "operation": operation,
        "result": "completed",
        "data_deleted": True,
        "data_remaining": False,
    }
    controller = _build_controller_double()
    handler = AsyncMock(return_value=result)
    setattr(controller, method_name, handler)
    app = internal_server.create_app(controller, memory_ui_secret=secret)
    headers = {
        MEMORY_USER_KEY_HEADER: "avibe:local",
        MEMORY_UI_PROOF_HEADER: build_ui_read_proof(
            secret,
            method="POST",
            path=path,
            user_key="avibe:local",
        ),
    }

    async def _exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(path, json={"confirm_loss": True}, headers=headers)

    response = asyncio.run(_exercise())
    assert response.status_code == 200
    assert response.json() == result
    handler.assert_awaited_once_with(confirm_loss=True)


def test_memory_reconfigure_forwards_the_cas_snapshot() -> None:
    from config.v2_config import MemoryConfig, memory_config_to_payload
    from vibe.memory_http_headers import MEMORY_USER_KEY_HEADER
    from vibe.memory_ui_access import MEMORY_UI_PROOF_HEADER, build_ui_read_proof

    secret = "test-memory-ui-secret"
    path = "/internal/memory/reconfigure"
    candidate = MemoryConfig(enabled=False, mode="custom")
    expected = MemoryConfig(enabled=False)
    controller = _build_controller_double()
    controller.reconfigure_memory = AsyncMock(
        return_value={"ok": True, "operation": "reconfigure"}
    )
    app = internal_server.create_app(controller, memory_ui_secret=secret)
    headers = {
        MEMORY_USER_KEY_HEADER: "avibe:local",
        MEMORY_UI_PROOF_HEADER: build_ui_read_proof(
            secret,
            method="POST",
            path=path,
            user_key="avibe:local",
        ),
    }

    async def _exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                path,
                headers=headers,
                json={
                    "confirm_loss": True,
                    "memory": memory_config_to_payload(candidate, include_secrets=True),
                    "expected_memory": memory_config_to_payload(
                        expected,
                        include_secrets=True,
                    ),
                },
            )

    response = asyncio.run(_exercise())

    assert response.status_code == 200
    controller.reconfigure_memory.assert_awaited_once_with(
        candidate,
        expected_config=expected,
        confirm_loss=True,
    )


@pytest.mark.parametrize(
    ("result", "expected_status"),
    [
        ({"ok": True, "result": "completed", "health": {}}, 200),
        (
            {
                "ok": False,
                "error": "memory_repair_not_required",
                "result": "unchanged",
            },
            409,
        ),
        (
            {
                "ok": False,
                "error": "memory_repair_failed",
                "result": "timed_out",
            },
            503,
        ),
    ],
)
def test_memory_repair_maps_controller_status(result: dict, expected_status: int) -> None:
    from vibe.memory_http_headers import MEMORY_USER_KEY_HEADER
    from vibe.memory_ui_access import MEMORY_UI_PROOF_HEADER, build_ui_read_proof

    secret = "test-memory-ui-secret"
    path = "/internal/memory/repair"
    user_key = "avibe:local"
    controller = _build_controller_double()
    controller.repair_memory = AsyncMock(return_value=result)
    app = internal_server.create_app(controller, memory_ui_secret=secret)

    async def _exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                path,
                json={"confirm_loss": True},
                headers={
                    MEMORY_USER_KEY_HEADER: user_key,
                    MEMORY_UI_PROOF_HEADER: build_ui_read_proof(
                        secret,
                        method="POST",
                        path=path,
                        user_key=user_key,
                    ),
                },
            )

    response = asyncio.run(_exercise())
    assert response.status_code == expected_status
    assert response.json() == result


def test_processing_record_degrades_signed_operator_lookup_failure() -> None:
    from vibe.memory_http_headers import MEMORY_USER_KEY_HEADER
    from vibe.memory_ui_access import MEMORY_UI_PROOF_HEADER, build_ui_read_proof

    secret = "test-memory-ui-secret"
    verified_user_keys: list[str | None] = []

    class Runtime:
        def principal_for_user_key(self, _user_key: str) -> str:
            raise MemoryStoreUnavailableError("Memory store is unavailable")

        async def processing_record_payload(
            self,
            *,
            verified_user_key: str | None = None,
        ) -> dict[str, object]:
            verified_user_keys.append(verified_user_key)
            return {
                "status": "ok",
                "runtime": {"source": {"status": "unavailable"}, "health": None},
                "sources": {},
                "anomalies": {"source": {"status": "unavailable"}, "items": []},
                "maintenance": {
                    "source": {"status": "unavailable"},
                    "can_clear": False,
                    "clear_in_progress": None,
                },
            }

    controller = _build_controller_double()
    controller.memory_runtime = Runtime()
    app = internal_server.create_app(controller, memory_ui_secret=secret)
    path = "/internal/memory/processing-record"
    user_key = "avibe:remote:subject-2"

    async def _exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            return await client.get(
                path,
                headers={
                    MEMORY_USER_KEY_HEADER: user_key,
                    MEMORY_UI_PROOF_HEADER: build_ui_read_proof(
                        secret,
                        method="GET",
                        path=path,
                        user_key=user_key,
                    ),
                },
            )

    response = asyncio.run(_exercise())

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert verified_user_keys == [user_key]


def test_processing_record_route_leaves_operator_lookup_to_runtime() -> None:
    from vibe.memory_http_headers import MEMORY_USER_KEY_HEADER
    from vibe.memory_ui_access import MEMORY_UI_PROOF_HEADER, build_ui_read_proof

    secret = "test-memory-ui-secret"
    verified_user_keys: list[str | None] = []

    class Runtime:
        def principal_for_user_key(self, _user_key: str) -> str:
            raise AssertionError("the socket route must not resolve Memory operators")

        async def processing_record_payload(
            self,
            *,
            verified_user_key: str | None = None,
        ) -> dict[str, object]:
            verified_user_keys.append(verified_user_key)
            return {
                "status": "ok",
                "runtime": {"source": {"status": "unavailable"}, "health": None},
                "sources": {},
                "anomalies": {"source": {"status": "available"}, "items": []},
                "maintenance": {"source": {"status": "available"}},
            }

    controller = _build_controller_double()
    controller.memory_runtime = Runtime()
    app = internal_server.create_app(controller, memory_ui_secret=secret)
    path = "/internal/memory/processing-record"
    user_key = "avibe:remote:subject-2"

    async def _exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            return await client.get(
                path,
                headers={
                    MEMORY_USER_KEY_HEADER: user_key,
                    MEMORY_UI_PROOF_HEADER: build_ui_read_proof(
                        secret,
                        method="GET",
                        path=path,
                        user_key=user_key,
                    ),
                },
            )

    response = asyncio.run(_exercise())

    assert response.status_code == 200
    assert verified_user_keys == [user_key]


def test_native_processing_record_routes_authorize_the_selected_project() -> None:
    from vibe.memory_http_headers import MEMORY_USER_KEY_HEADER
    from vibe.memory_ui_access import MEMORY_UI_PROOF_HEADER, build_ui_read_proof

    principal_id = "u-11111111111111111111111111111111"
    runtime = SimpleNamespace(
        resolve_principal_for_user_key=AsyncMock(return_value=principal_id),
        list_memory_projects=AsyncMock(return_value=("default", "notes")),
        processing_record_entries_payload=AsyncMock(
            return_value={"status": "ok", "entries": [], "next_cursor": None}
        ),
        processing_record_entry_payload=AsyncMock(
            return_value={"status": "ok", "entry": {"memcell_id": "mc_1"}}
        ),
    )
    controller = _build_controller_double()
    controller.default_memory_project_id.return_value = "default"
    controller.memory_runtime = runtime
    secret = "test-memory-ui-secret"
    user_key = "avibe:local"
    app = internal_server.create_app(controller, memory_ui_secret=secret)

    def headers(path: str) -> dict[str, str]:
        return {
            MEMORY_USER_KEY_HEADER: user_key,
            MEMORY_UI_PROOF_HEADER: build_ui_read_proof(
                secret,
                method="GET",
                path=path,
                user_key=user_key,
            ),
        }

    async def _exercise() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            list_path = "/internal/memory/processing-record/entries"
            detail_path = "/internal/memory/processing-record/entry"
            listed = await client.get(
                f"{list_path}?project=notes&limit=17",
                headers=headers(list_path),
            )
            detail = await client.get(
                f"{detail_path}?memcell_id=mc_1&project=notes",
                headers=headers(detail_path),
            )
            unknown = await client.get(
                f"{list_path}?project=unknown&limit=17",
                headers=headers(list_path),
            )
            return listed, detail, unknown

    listed, detail, unknown = asyncio.run(_exercise())

    assert listed.status_code == 200
    assert detail.status_code == 200
    assert unknown.status_code == 400
    runtime.processing_record_entries_payload.assert_awaited_once_with(
        principal_id, "notes", None, 17
    )
    runtime.processing_record_entry_payload.assert_awaited_once_with(
        principal_id, "notes", "mc_1"
    )
    assert runtime.list_memory_projects.await_count == 3


def test_memory_remember_route_does_not_hold_pointer_lock_during_capture() -> None:
    """Long capture work runs after the Controller pointer snapshot."""

    from core.controller import Controller
    from avibe_memory import CaptureAccepted
    from vibe.memory_http_headers import CALLER_SESSION_HEADER

    class Module:
        def __init__(self) -> None:
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def capture(self, _request):  # noqa: ANN001, ANN202
            assert controller._memory_replacement_lock().locked() is False
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return CaptureAccepted()

    module = Module()
    runtime = SimpleNamespace(available=True, module=module)
    controller = Controller.__new__(Controller)
    controller.config = SimpleNamespace(
        memory=SimpleNamespace(enabled=True),
    )
    controller.memory_runtime = runtime
    controller.memory_scope_for_cli_session = lambda _session_id: (
        "u-" + "1" * 32,
        "p-" + "2" * 32,
    )
    app = internal_server.create_app(controller)

    async def _exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            request = asyncio.create_task(
                client.post(
                    "/internal/memory/remember",
                    json={"text": "remember this"},
                    headers={CALLER_SESSION_HEADER: "session-1"},
                )
            )
            await module.started.wait()
            async with asyncio.timeout(0.1):
                async with controller._memory_replacement_lock():
                    assert controller.memory_runtime is runtime
            module.release.set()
            return await request

    response = asyncio.run(_exercise())

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    assert module.calls == 1


def test_memory_remember_accepts_text_over_legacy_controller_limit() -> None:
    """MEMORY-SEARCH-018: the internal socket delegates large remember text."""

    from avibe_memory import CaptureAccepted
    from vibe.memory_http_headers import CALLER_SESSION_HEADER

    text = "remember this detail " * 300
    controller = _build_controller_double()
    controller.memory_scope_for_cli_session.return_value = (
        "u-11111111111111111111111111111111",
        "default",
    )
    controller.memory_runtime = SimpleNamespace()
    controller.capture_memory = AsyncMock(return_value=CaptureAccepted())
    app = internal_server.create_app(controller)

    async def _exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            return await client.post(
                "/internal/memory/remember",
                json={"text": text},
                headers={CALLER_SESSION_HEADER: "session-1"},
            )

    response = asyncio.run(_exercise())

    assert len(text) > 4_000
    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    request = controller.capture_memory.await_args.args[0]
    assert request.text == text


# ---------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------


def test_default_socket_path_lives_under_state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.delenv("VIBE_INTERNAL_DISPATCH_SOCKET", raising=False)
    path = internal_server.default_socket_path()
    assert path.name == "dispatch.sock"
    assert tmp_path in path.parents


def test_default_socket_path_honors_env_override(monkeypatch, tmp_path):
    target = tmp_path / "runtime" / "dispatch.sock"
    monkeypatch.setenv("VIBE_INTERNAL_DISPATCH_SOCKET", str(target))

    assert internal_server.default_socket_path() == target


def test_bind_socket_prebinds_unix_listener():
    # macOS has a short sockaddr_un limit, and pytest tmp paths can exceed it.
    with tempfile.TemporaryDirectory(prefix="vr-") as tmp:
        target = Path(tmp) / "dispatch.sock"
        listener, bound = internal_server._bind_socket(target)

        try:
            assert bound == target.resolve()
            assert target.exists()
            assert listener.family == socket.AF_UNIX
            assert listener.type == socket.SOCK_STREAM
        finally:
            listener.close()
            target.unlink(missing_ok=True)


def test_create_app_exposes_minimal_endpoints():
    app = internal_server.create_app(_build_controller_double())
    routes = {(r.path, tuple(sorted(r.methods))) for r in app.routes if hasattr(r, "methods")}
    # All interactive sources use the fire-and-forget turn entry.
    assert ("/internal/health", ("GET",)) in routes
    assert ("/internal/dispatch_async", ("POST",)) in routes
    assert ("/internal/reconcile-platforms", ("POST",)) in routes
    assert ("/internal/reconcile-agent-backends", ("POST",)) in routes
    assert ("/internal/model-hub", ("POST",)) in routes
    assert ("/internal/cancel/{session_id}", ("POST",)) in routes
    assert ("/internal/dispatch", ("POST",)) not in routes
    assert ("/internal/events", ("GET",)) in routes
    assert ("/internal/events", ("POST",)) in routes


# ---------------------------------------------------------------------
# ASGI round-trips
# ---------------------------------------------------------------------


async def _health_round_trip():
    app = internal_server.create_app(_build_controller_double())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/internal/health")
    return resp


def test_health_endpoint():
    resp = asyncio.run(_health_round_trip())
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "service": "vibe-remote-internal", "version": 1}


def test_model_hub_rpc_uses_controller_owned_service(monkeypatch):
    monkeypatch.setenv("VIBE_MODEL_HUB_ENABLED", "1")
    controller = _build_controller_double()
    controller.model_hub_service = MagicMock()
    controller.model_hub_service.list_sources.return_value = [{"id": "src_owned"}]
    app = internal_server.create_app(controller)

    async def _go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/internal/model-hub",
                json={"operation": "list_sources", "payload": {}},
            )

    response = asyncio.run(_go())

    assert response.status_code == 200
    assert response.json() == {"ok": True, "result": [{"id": "src_owned"}]}
    controller.model_hub_service.list_sources.assert_called_once_with()


def test_model_hub_rpc_is_stably_disabled_without_touching_service(monkeypatch):
    monkeypatch.delenv("VIBE_MODEL_HUB_ENABLED", raising=False)
    controller = _build_controller_double()
    controller.model_hub_service = MagicMock()
    app = internal_server.create_app(controller)

    async def _go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/internal/model-hub",
                json={"operation": "list_sources", "payload": {}},
            )

    response = asyncio.run(_go())

    assert response.status_code == 404
    assert response.json() == {
        "ok": False,
        "contract_version": 1,
        "error": "feature_disabled",
    }
    assert controller.model_hub_service.mock_calls == []


def test_model_hub_dependency_rpc_remains_available_when_feature_is_disabled(monkeypatch):
    from core.handlers.model_hub.adapter import (
        EngineEnsureResult,
        EngineHealth,
        EngineStatus,
    )

    monkeypatch.delenv("VIBE_MODEL_HUB_ENABLED", raising=False)
    controller = _build_controller_double()
    controller.model_hub_service = None
    controller.model_hub_engine_adapter = SimpleNamespace(
        ensure_installed=AsyncMock(
            return_value=EngineEnsureResult(
                status=EngineStatus(
                    health=EngineHealth.NOT_STARTED,
                    installed_version="v7.2.149",
                    verified=True,
                    listen_host="127.0.0.1",
                    listen_port=None,
                    last_check_iso="2026-09-05T00:00:00Z",
                    host_platform="linux-amd64",
                ),
                changed=True,
            )
        )
    )
    app = internal_server.create_app(controller)

    async def _go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/internal/model-hub",
                json={
                    "operation": "runtime_ensure_dependency",
                    "payload": {"force": True, "offline": True},
                },
            )

    response = asyncio.run(_go())

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["enabled"] is False
    assert result["changed"] is True
    assert result["status"]["installed_version"] == "v7.2.149"
    controller.model_hub_engine_adapter.ensure_installed.assert_awaited_once_with(
        force=True,
        offline=True,
    )


async def _publish_event_round_trip():
    from core import inbox_events

    app = internal_server.create_app(_build_controller_double())
    transport = httpx.ASGITransport(app=app)
    sub_id, queue = inbox_events.bus.subscribe()
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/internal/events",
                json={
                    "type": "vaults.updated",
                    "data": {"scope": "request", "request_id": "vreq_1", "request_status": "pending"},
                },
            )
            queue_resp = await client.post(
                "/internal/events",
                json={
                    "type": "queue.updated",
                    "data": {"session_id": "ses_queue"},
                },
            )
            definitions_resp = await client.post(
                "/internal/events",
                json={
                    "type": "definitions.updated",
                    "data": {"definition_type": "scheduled"},
                },
            )
            bad_resp = await client.post("/internal/events", json={"type": "unsupported", "data": {}})
        events = [
            await asyncio.wait_for(queue.get(), timeout=1.0),
            await asyncio.wait_for(queue.get(), timeout=1.0),
            await asyncio.wait_for(queue.get(), timeout=1.0),
        ]
        return resp, queue_resp, definitions_resp, bad_resp, events
    finally:
        inbox_events.bus.unsubscribe(sub_id)


def test_publish_event_endpoint_emits_allowlisted_bus_event():
    resp, queue_resp, definitions_resp, bad_resp, events = asyncio.run(
        _publish_event_round_trip()
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert queue_resp.status_code == 200
    assert queue_resp.json() == {"ok": True}
    assert definitions_resp.status_code == 200
    assert definitions_resp.json() == {"ok": True}
    assert bad_resp.status_code == 400
    assert events == [
        (
            "vaults.updated",
            {"scope": "request", "request_id": "vreq_1", "request_status": "pending"},
        ),
        ("queue.updated", {"session_id": "ses_queue"}),
        ("definitions.updated", {"definition_type": "scheduled"}),
    ]


def test_internal_events_end_a_subscriber_whose_queue_overflowed(monkeypatch):
    """The controller feed drops its reader rather than serving it a hole.

    This is the first bounded queue on the controller → UI-server → browser
    path, and a discard here is the most invisible of the three: the UI server's
    broker never sees the event, so every id downstream stays contiguous and
    every socket keeps heartbeating. Ending the stream is what turns the loss
    into a signal -- the bridge reconnects, the reconnect flips
    ``workbench.events.bridge.status``, and browsers reconcile once. Announcing
    the hole down this same stream cannot work: the queue is still full, so the
    next iteration finds another discard and announces again, starving the
    payload frames the warning was about.
    """

    from core import inbox_events

    subscriptions: list[tuple[int, asyncio.Queue]] = []
    real_subscribe = inbox_events.bus.subscribe

    def tracking_subscribe():
        # The controller handshake carries no sub_id (the browser feed's does),
        # so the test learns it the only other way available to a caller.
        subscription = real_subscribe()
        subscriptions.append(subscription)
        return subscription

    monkeypatch.setattr(inbox_events.bus, "subscribe", tracking_subscribe)
    baseline_subscribers = inbox_events.bus.subscriber_count()

    def decode(chunk) -> str:
        return chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk

    async def collect_until_end() -> tuple[str, int, list[str], bool, int]:
        app = internal_server.create_app(_build_controller_double())
        route = next(
            candidate
            for candidate in app.routes
            if getattr(candidate, "path", None) == "/internal/events"
            and "GET" in (getattr(candidate, "methods", None) or set())
        )
        response = await route.endpoint()
        iterator = response.body_iterator.__aiter__()
        frames: list[str] = []
        ended = False
        try:
            handshake = decode(await iterator.__anext__())
            sub_id, queue = subscriptions[-1]
            # Park the stream inside its read loop before overflowing the queue,
            # so what this asserts is a discard the reader has to notice on a
            # later iteration -- not one already covered by the handshake it
            # just delivered.
            pending = asyncio.create_task(iterator.__anext__())
            await asyncio.sleep(0)

            for index in range(queue.maxsize + 3):
                inbox_events.bus.publish("runs.updated", {"run_id": f"run_{index}"})
            # One yield runs the whole batch of ``call_soon_threadsafe``
            # handoffs, so every discard has happened by the time the count is
            # read.
            await asyncio.sleep(0)
            dropped = inbox_events.bus.dropped_count(sub_id)

            # Bounded well below the ``maxsize`` frames still sitting in the
            # queue: a stream that kept serving them would run out of the budget
            # rather than end, which is the failure this asserts against.
            for _ in range(5):
                awaitable = pending if pending is not None else iterator.__anext__()
                pending = None
                try:
                    frames.append(decode(await asyncio.wait_for(awaitable, timeout=2)))
                except StopAsyncIteration:
                    ended = True
                    break
        finally:
            await iterator.aclose()
        # Read after the generator has exited: its ``finally`` unsubscribes, and
        # a subscription that no longer exists is owed nothing.
        return handshake, dropped, frames, ended, inbox_events.bus.subscriber_count()

    handshake, dropped, frames, ended, subscribers_after = asyncio.run(collect_until_end())

    assert "event: connected" in handshake
    assert dropped == 3
    assert ended is True
    # The queue still held ``maxsize`` events, so anything but a short tail here
    # means the reader kept relaying frames across the gap.
    assert len(frames) <= 1
    assert subscribers_after == baseline_subscribers


def test_reconcile_platforms_endpoint_calls_controller(monkeypatch):
    controller = _build_controller_double()
    calls = []

    async def reconcile_platforms(config):
        calls.append(config)
        return {"ok": True, "added": ["discord"]}

    controller.reconcile_platforms = reconcile_platforms
    monkeypatch.setattr("config.v2_config.V2Config.load", lambda: "v2-config")
    monkeypatch.setattr("config.v2_compat.to_app_config", lambda config: f"compat:{config}")
    app = internal_server.create_app(controller)

    async def _go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post("/internal/reconcile-platforms")

    resp = asyncio.run(_go())

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "added": ["discord"]}
    assert calls == ["compat:v2-config"]


def test_reconcile_platforms_endpoint_reports_controller_failure(monkeypatch):
    controller = _build_controller_double()

    async def reconcile_platforms(config):
        raise RuntimeError("IM thread for discord did not stop within timeout")

    controller.reconcile_platforms = reconcile_platforms
    monkeypatch.setattr("config.v2_config.V2Config.load", lambda: "v2-config")
    monkeypatch.setattr("config.v2_compat.to_app_config", lambda config: f"compat:{config}")
    app = internal_server.create_app(controller)

    async def _go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post("/internal/reconcile-platforms")

    resp = asyncio.run(_go())

    assert resp.status_code == 500
    assert resp.json() == {"ok": False, "error": "IM thread for discord did not stop within timeout"}


def test_invalidate_activity_streaming_endpoint_clears_controller_cache(monkeypatch):
    controller = _build_controller_double()
    calls: list[bool] = []
    monkeypatch.setattr(
        "core.message_mirror.reset_activity_flag_cache",
        lambda: calls.append(True),
    )
    app = internal_server.create_app(controller)

    async def _go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post("/internal/invalidate-activity-streaming")

    resp = asyncio.run(_go())

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert calls == [True]


def test_reconcile_agent_backends_endpoint_calls_controller():
    controller = _build_controller_double()
    calls = []

    async def reconcile_agent_backends(backends):
        calls.append(backends)
        return {
            "ok": True,
            "backends": backends,
            "states": {backend: "restarted" for backend in backends},
        }

    controller.reconcile_agent_backends = reconcile_agent_backends
    app = internal_server.create_app(controller)

    async def _go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/internal/reconcile-agent-backends",
                json={"backends": ["codex", "opencode"]},
            )

    resp = asyncio.run(_go())

    assert resp.status_code == 200
    assert resp.json()["states"] == {
        "codex": "restarted",
        "opencode": "restarted",
    }
    assert calls == [["codex", "opencode"]]


def test_reconcile_agent_backends_endpoint_rejects_invalid_shape():
    app = internal_server.create_app(_build_controller_double())

    async def _go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/internal/reconcile-agent-backends",
                json={"backends": "codex"},
            )

    resp = asyncio.run(_go())

    assert resp.status_code == 400
    assert resp.json() == {
        "ok": False,
        "error": "backends must be a list of strings",
    }


def test_backend_auth_endpoint_uses_controller_owned_service():
    controller = _build_controller_double()
    test_web_auth = AsyncMock(return_value={"ok": True, "excerpt": "hello"})
    controller.agent_auth_service = SimpleNamespace(test_web_auth=test_web_auth)
    app = internal_server.create_app(controller)

    async def _go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/internal/backend-auth/test",
                json={"backend": "Codex", "model": "gpt-5.4-mini"},
            )

    resp = asyncio.run(_go())

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "excerpt": "hello"}
    test_web_auth.assert_awaited_once_with("codex", model="gpt-5.4-mini")


def test_backend_auth_endpoint_rejects_invalid_shape():
    app = internal_server.create_app(_build_controller_double())

    async def _go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/internal/backend-auth/test",
                json={"backend": "codex", "model": ["gpt-5.4-mini"]},
            )

    resp = asyncio.run(_go())

    assert resp.status_code == 400
    assert resp.json() == {"ok": False, "error": "model must be a string"}


async def _dispatch_round_trip(body: dict) -> httpx.Response:
    app = internal_server.create_app(_build_controller_double())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post("/internal/dispatch_async", json=body)


def test_dispatch_rejects_missing_text():
    # Payload validation runs before any turn/queue work, so a bad request 400s
    # the same way on the fire-and-forget endpoint.
    resp = asyncio.run(_dispatch_round_trip({"session_id": "s1"}))
    assert resp.status_code == 400
    payload = resp.json()
    assert payload["ok"] is False
    assert "text" in payload["error"]


def test_dispatch_rejects_missing_session_id():
    resp = asyncio.run(_dispatch_round_trip({"text": "hi"}))
    assert resp.status_code == 400
    assert "session_id" in resp.json()["error"]


def test_dispatch_context_does_not_restore_memory_admission_from_transient_payload(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _engine, session = _create_test_session(
        tmp_path,
        native_id="proj_memory_classification",
    )

    _text, context = asyncio.run(
        internal_server._build_dispatch_payload(
            {
                "session_id": session["id"],
                "text": "remember this",
                "author_id": "remote:authenticated",
                "user_id": "remote:forged-memory-principal",
                "message_kind": "original",
                "memory_cli_admitted": True,
                "is_ordinary_text": True,
            }
        )
    )

    assert context.user_id == "remote:authenticated"
    assert context.message_kind == "original"
    assert context.is_original_human_text is True
    assert "memory_cli_admitted" not in (context.platform_specific or {})


def test_register_turn_sink_ignores_duplicate_and_pop_is_identity_guarded():
    """Streaming turns are serialized per session (dispatch_turn rejects a
    concurrent one). As defense in depth, register_turn_sink must NOT clobber
    an in-flight sink, and pop_turn_sink must only remove the sink whose
    done_event matches the caller's — so no stale turn can satisfy or evict
    another turn's sink. The sink registry is owned by SessionTurnManager (the
    Controller methods are thin delegations)."""
    mgr = session_turns.SessionTurnManager()
    first = asyncio.Event()
    mgr.register_turn_sink("avibe::s", on_chunk=AsyncMock(), done_event=first)
    second = asyncio.Event()
    mgr.register_turn_sink("avibe::s", on_chunk=AsyncMock(), done_event=second)

    # The in-flight sink is kept; the duplicate is dropped and NOT released.
    assert mgr.active_turn_sinks["avibe::s"]["done_event"] is first
    assert not first.is_set()

    # pop is identity-guarded: a non-matching done_event is a no-op.
    mgr.pop_turn_sink("avibe::s", second)
    assert "avibe::s" in mgr.active_turn_sinks
    mgr.pop_turn_sink("avibe::s", first)
    assert "avibe::s" not in mgr.active_turn_sinks


def test_dispatch_rejects_concurrent_same_session_turn():
    """dispatch_turn serializes per session: when a streaming turn is already
    in flight (a sink is registered), a second streaming dispatch is refused
    with a terminal error chunk and never starts a competing agent turn —
    so two streams can't race over one session and cross-feed."""
    chunks: list[dict] = []

    async def on_chunk(env):
        chunks.append(env)

    handler_calls: list = []

    async def handler(ctx, text):
        handler_calls.append(text)

    controller = _build_controller_double(handler=handler)
    controller._t = lambda key, **kw: f"i18n:{key}"
    ctx = MessageContext(user_id="U", channel_id="C", platform="avibe")
    # Simulate a streaming turn already in flight for this session.
    controller.register_turn_sink(
        controller._get_session_key(ctx), on_chunk=AsyncMock(), done_event=asyncio.Event()
    )

    asyncio.run(dispatch_turn(controller, ctx, "second", on_chunk=on_chunk))

    assert handler_calls == [], "a concurrent turn must not start the agent"
    assert any(c.get("kind") == "error" for c in chunks), "a terminal error chunk must be emitted"


def test_dispatch_forwards_session_routing_into_platform_specific(monkeypatch, tmp_path):
    """Regression for the Codex P1: ``/internal/dispatch_async`` must hand the
    workbench session's agent / model / effort to ``MessageHandler`` via
    ``platform_specific["agent_session_target"]`` + ``vibe_agent_name``
    so the Chat header's chosen agent is actually used instead of the
    controller's default routing.
    """

    from core.services import sessions as sessions_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_routing",
            now="2026-05-26T13:00:00Z",
        )
        _seed_project_workdir(conn, scope_id, tmp_path, now="2026-05-26T13:00:00Z")
        session = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="claude",
            agent_name="contract-bot",
            model="claude-sonnet-4-6",
            reasoning_effort="high",
            metadata={
                "created_via": "session_fork",
                "fork_source_session_id": "ses-source",
                "fork_source_native_session_id": "thread-source",
                "fork_source_backend": "claude",
            },
        )
        delivery = _reserve_submission(
            conn,
            scope_id=scope_id,
            session_id=session["id"],
            text="hi",
        )
    session_id = session["id"]

    captured: dict = {}

    async def capture(ctx, text):
        captured["platform_specific"] = dict(ctx.platform_specific or {})
        # Release the held turn the way a real result emit would so the
        # fire-and-forget dispatch settles promptly.
        controller.mark_turn_complete(ctx)

    controller = _build_controller_double(handler=capture)
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/internal/dispatch_async",
                json={
                    "session_id": session_id,
                    "text": "hi",
                    "user_message_id": delivery["id"],
                },
            )
            assert resp.status_code == 202
        # Fire-and-forget: wait for the background turn to run + capture.
        for _ in range(200):
            if "platform_specific" in captured and session_id not in app.state.in_flight_dispatches:
                break
            await asyncio.sleep(0.02)

    asyncio.run(_go())
    payload = captured["platform_specific"]
    assert payload.get("workbench_session_id") == session_id
    assert payload.get("vibe_agent_name") == "contract-bot"
    target = payload.get("agent_session_target") or {}
    assert target.get("agent_name") == "contract-bot"
    assert target.get("agent_backend") == "claude"
    assert target.get("model") == "claude-sonnet-4-6"
    assert target.get("reasoning_effort") == "high"
    # session_anchor is carried so resume binds by the stored anchor after a
    # restart instead of a computed avibe_<id> (Codex P2). Workbench sessions
    # self-anchor to their id.
    assert target.get("session_anchor") == session_id
    assert target.get("metadata", {}).get("fork_source_native_session_id") == "thread-source"


def test_dispatch_async_starts_turn_and_returns_202(monkeypatch, tmp_path):
    """The fire-and-forget path starts the turn and returns 202 immediately.
    It still holds the turn open (via a no-op on_chunk) so ``in_flight`` is set
    for the turn's lifetime, then released when the turn completes — the reply
    itself reaches the browser over ``message.new``, not this response.

    It also publishes the session-level ``turn.start`` / ``turn.end`` lifecycle
    on the inbox bus (the browser's working-indicator signal)."""
    from core import inbox_events
    from storage.importer import ensure_sqlite_state

    # dispatch_async reads the queue (to preserve order after a Stop), so it needs
    # an initialized state DB even on the empty-queue happy path.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session = _create_test_session(tmp_path, native_id="proj_dispatch_start")
    with engine.begin() as conn:
        delivery = _reserve_submission(
            conn,
            scope_id=session["scope_id"],
            session_id=session["id"],
            text="hi",
        )
    session_id = session["id"]

    started = asyncio.Event()

    async def handler(ctx, text):
        started.set()
        # Release the held turn the way a real result emit would.
        controller.mark_turn_complete(ctx)
        return None

    controller = _build_controller_double(handler=handler)
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)

    async def _go():
        sub_id, queue = inbox_events.bus.subscribe()
        events: list[str] = []
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                resp = await client.post(
                    "/internal/dispatch_async",
                    json={
                        "session_id": session_id,
                        "text": "hi",
                        "user_message_id": delivery["id"],
                    },
                )
            await asyncio.wait_for(started.wait(), timeout=3)
            for _ in range(100):
                if session_id not in app.state.in_flight_dispatches:
                    break
                await asyncio.sleep(0.02)
            # Materialization also emits message.new; collect until the
            # lifecycle closes instead of assuming adjacent bus events.
            for _ in range(12):
                try:
                    evt, _data = await asyncio.wait_for(queue.get(), timeout=1.0)
                    events.append(evt)
                    if evt == "turn.end":
                        break
                except asyncio.TimeoutError:
                    break
        finally:
            inbox_events.bus.unsubscribe(sub_id)
        return resp, events

    resp, events = asyncio.run(_go())
    assert resp.status_code == 202
    assert resp.json()["ok"] is True
    controller.message_handler.handle_user_message.assert_awaited()
    assert session_id not in app.state.in_flight_dispatches, "slot released after the turn"
    assert [event for event in events if event.startswith("turn.")] == [
        "turn.start",
        "turn.end",
    ], "publishes session turn lifecycle on the bus"


def test_dispatch_async_stop_receipt_waits_for_terminal_evidence(monkeypatch, tmp_path):
    """There is NO turn-duration timeout (Phase 1a): a turn whose backend never
    emits a terminal result stays in_flight indefinitely — the slot is freed ONLY
    by a real terminal result or a cancel, never by any timer. A long-running
    agent can run for hours and must keep its Stop control the whole time.

    We patch ``dispatch_turn`` to a coroutine that just sleeps (never fires the
    turn's done_event), confirm the session is still held in_flight after a beat,
    then cancel to clean up.
    """
    from storage.importer import ensure_sqlite_state

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session = _create_test_session(tmp_path, native_id="proj_dispatch_long")
    with engine.begin() as conn:
        delivery = _reserve_submission(
            conn,
            scope_id=session["scope_id"],
            session_id=session["id"],
            text="hi",
        )
    session_id = session["id"]

    started = asyncio.Event()

    async def _never_settles(
        ctrl, ctx, text, *, source=SOURCE_HUMAN, on_chunk=None, **_kwargs
    ):
        # Model a long agent turn: the backend accepted the prompt but hasn't
        # produced its terminal result yet. dispatch_turn would normally hold on
        # ``await done.wait()`` with no timeout — emulate that by just sleeping so
        # the turn never settles on its own.
        started.set()
        await asyncio.sleep(60)
        return TurnDispatchOutcome(error=None, settled_by=SETTLED_BY_TERMINAL_RESULT)

    monkeypatch.setattr(session_turns, "dispatch_turn_with_outcome", _never_settles)

    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)
    captured: dict = {}

    async def _go():
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/internal/dispatch_async",
                json={
                    "session_id": session_id,
                    "text": "hi",
                    "user_message_id": delivery["id"],
                },
            )
            assert resp.status_code == 202
            await asyncio.wait_for(started.wait(), timeout=3)
            # Give any (nonexistent) timer ample time to fire, then confirm the slot
            # is STILL held — no timer auto-freed it.
            await asyncio.sleep(0.1)
            entry = app.state.in_flight_dispatches.get(session_id)
            captured["held"] = entry is not None and not entry.task.done()
            # Only a real cancel frees the slot — clean up so the loop tears down.
            resp_cancel = await client.post(f"/internal/cancel/{session_id}")
            captured["cancel_status"] = resp_cancel.status_code
            await asyncio.sleep(0.05)
            captured["held_after_stop_receipt"] = (
                session_id in app.state.in_flight_dispatches
            )
            turn = app.state.in_flight_dispatches[session_id]
            turn.task.cancel()
            await asyncio.gather(turn.task, return_exceptions=True)

    asyncio.run(_go())
    assert captured["held"] is True, "a turn with no terminal result is NOT auto-freed by any timer"
    assert captured["cancel_status"] == 200, "the user's Stop ends the wedged turn"
    assert captured["held_after_stop_receipt"] is True


def test_dispatch_async_enqueues_during_busy_turn(monkeypatch, tmp_path):
    """A dispatch for a session that already has a turn in flight ENQUEUES
    (send-while-busy) instead of refusing: it atomically re-types the
    pre-persisted user row as queued and returns 202 {queued}, and never starts
    a competing agent turn. The row flushes when the running turn ends."""
    from core.services import sessions as sessions_service

    from storage import message_deliveries, messages_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn, platform="avibe", scope_type="project", native_id="proj_enq", now="2026-05-31T00:00:00Z"
        )
        _seed_project_workdir(conn, scope_id, tmp_path)
        session = sessions_service.create_session(
            conn, scope_id=scope_id, agent_backend="claude", agent_name="worker"
        )
        owner = _reserve_submission(
            conn,
            scope_id=scope_id,
            session_id=session["id"],
            text="active owner",
        )
        owner_turn_id = message_deliveries.new_turn_id()
        message_deliveries.insert_turn(
            conn,
            turn_id=owner_turn_id,
            session_id=session["id"],
            initial_delivery_id=owner["id"],
            state="active",
            backend="claude",
        )
        user_row = _reserve_submission(
            conn,
            scope_id=scope_id,
            session_id=session["id"],
            text="while busy",
        )
    session_id = session["id"]

    from core.inbox_events import bus

    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async def _busy():
            await asyncio.sleep(60)

        task = asyncio.create_task(_busy())
        app.state.in_flight_dispatches[session_id] = session_turns.Turn(
            task=task,
            context=MessageContext(user_id="U", channel_id="C", platform="avibe"),
            logical_turn_id=owner_turn_id,
        )
        sub_id, events = bus.subscribe()
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                resp = await client.post(
                    "/internal/dispatch_async",
                    json={"session_id": session_id, "text": "while busy", "user_message_id": user_row["id"]},
                )
        finally:
            task.cancel()
        # bus.publish defers delivery via loop.call_soon_threadsafe; yield so the
        # scheduled puts land before we drain.
        await asyncio.sleep(0.05)
        published = []
        while not events.empty():
            published.append(events.get_nowait())
        bus.unsubscribe(sub_id)
        return resp, published

    resp, published = asyncio.run(_go())
    assert resp.status_code == 202
    assert resp.json()["queued"] is True
    controller.message_handler.handle_user_message.assert_not_awaited()
    # Enqueue surfaces the queue growth immediately so the UI reflects it without
    # waiting for the flush (queue.updated-on-enqueue, #3336001455).
    assert ("queue.updated", {"session_id": session_id}) in published
    with engine.connect() as conn:
        # The row was atomically re-typed to queued (now out of the transcript).
        assert [q["text"] for q in message_deliveries.list_queued(conn, session_id)] == ["while busy"]
        transcript = messages_service.list_session_messages(conn, session_id=session_id, types=("user",))
    assert transcript["messages"] == []


def test_dispatch_async_replay_of_queued_delivery_is_idempotent(monkeypatch, tmp_path):
    from storage import message_deliveries, messages_service

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session = _create_test_session(tmp_path, native_id="proj_replay_queued")
    with engine.begin() as conn:
        row = message_deliveries.enqueue_queued(
            conn,
            scope_id=session["scope_id"],
            session_id=session["id"],
            text="same show event",
        )

    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post(
                "/internal/dispatch_async",
                json={
                    "session_id": session["id"],
                    "text": "same show event",
                    "user_message_id": row["id"],
                },
            )

    response = asyncio.run(_go())

    assert response.status_code == 202
    assert response.json()["duplicate"] is True
    assert response.json()["queued"] is True
    controller.message_handler.handle_user_message.assert_not_awaited()
    with engine.connect() as conn:
        transcript = messages_service.list_session_messages(
            conn,
            session_id=session["id"],
        )["messages"]
    assert transcript == []


def test_dispatch_async_starts_authorized_remote_reservation(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session = _create_test_session(
        tmp_path,
        native_id="proj_remote_dispatch_reservation",
    )
    _seed_remote_worker()
    with engine.begin() as conn:
        remote = _reserve_submission(
            conn,
            scope_id=session["scope_id"],
            session_id=session["id"],
            text="remote reserved input",
            metadata=_authorized_remote_message_metadata(),
        )

    controller = None

    async def handler(context, _text):
        controller.mark_turn_complete(context)

    controller = _build_controller_double(handler)
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post(
                "/internal/dispatch_async",
                json={
                    "session_id": session["id"],
                    "text": "remote reserved input",
                    "user_message_id": remote["id"],
                },
            )

    response = asyncio.run(_go())

    assert response.status_code == 202
    assert response.json()["ok"] is True
    controller.message_handler.handle_user_message.assert_awaited_once()
    with engine.connect() as conn:
        stored = message_deliveries.get_delivery(conn, remote["id"])
    assert stored is not None
    assert stored["state"] == "accepted"


def test_dispatch_async_restores_message_kind_from_reserved_delivery(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session = _create_test_session(
        tmp_path,
        native_id="proj_durable_message_kind",
    )
    with engine.begin() as conn:
        reserved = _reserve_submission(
            conn,
            scope_id=session["scope_id"],
            session_id=session["id"],
            text="durable quick reply",
            message_kind="quick_reply",
        )

    observed: list[MessageContext] = []
    dispatch_kinds: list[object] = []
    build_dispatch_payload = internal_server._build_dispatch_payload

    async def observe_dispatch_payload(payload):
        dispatch_kinds.append(payload.get("message_kind"))
        return await build_dispatch_payload(payload)

    monkeypatch.setattr(
        internal_server,
        "_build_dispatch_payload",
        observe_dispatch_payload,
    )
    controller = None

    async def handler(context, _text):
        observed.append(context)
        controller.mark_turn_complete(context)

    controller = _build_controller_double(handler)
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post(
                "/internal/dispatch_async",
                json={
                    "session_id": session["id"],
                    "text": "stale request text",
                    "user_message_id": reserved["id"],
                    "message_kind": "original",
                },
            )

    response = asyncio.run(_go())

    assert response.status_code == 202
    assert dispatch_kinds == ["quick_reply"]
    assert len(observed) == 1
    assert observed[0].message_kind == "quick_reply"
    assert observed[0].is_original_human_text is False


def test_dispatch_async_persists_acceptance_before_a_lost_response_replay(
    monkeypatch,
    tmp_path,
):
    from core.inbox_events import bus
    from core.services import sessions as sessions_service
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_lost_acceptance_response",
            now="2026-05-31T00:00:00Z",
        )
        _seed_project_workdir(conn, scope_id, tmp_path)
        session = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="claude",
            agent_name="worker",
        )
        row = _reserve_submission(
            conn,
            scope_id=scope_id,
            session_id=session["id"],
            author="harness",
            source="harness",
            message_type="harness",
            text="Dispatch exactly once.",
        )

    controller = None
    dispatched_text: list[str] = []

    async def handler(context, text):
        dispatched_text.append(text)
        controller.mark_turn_complete(context)

    controller = _build_controller_double(handler)
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        bus,
        "publish",
        lambda event_type, data: published.append((event_type, data)),
    )
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)
    payload = {
        "session_id": session["id"],
        "text": "stale caller text must not replace the reservation",
        "user_message_id": row["id"],
    }

    async def _go():
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            first = await client.post("/internal/dispatch_async", json=payload)
            for _ in range(200):
                if session["id"] not in app.state.in_flight_dispatches:
                    break
                await asyncio.sleep(0.01)
            replay = await client.post("/internal/dispatch_async", json=payload)
        return first, replay

    first, replay = asyncio.run(_go())

    assert first.status_code == 202
    assert first.json()["delivery_state"] in {"claimed", "accepted"}
    assert replay.status_code == 202
    assert replay.json()["duplicate"] is True
    assert dispatched_text == ["Dispatch exactly once."]
    controller.message_handler.handle_user_message.assert_awaited_once()
    with engine.connect() as conn:
        settled = messages_service.get_message(conn, row["id"], session_id=session["id"])
    assert settled is not None
    assert settled["type"] == messages_service.HARNESS_TYPE
    assert [
        data["id"]
        for event_type, data in published
        if event_type == "message.new"
    ] == [row["id"]]


def test_dispatch_async_rejects_archived_session_after_reservation_reclaimed(
    monkeypatch,
    tmp_path,
):
    from core.services import sessions as sessions_service
    from storage import messages_service, workbench_sessions_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_archived_dispatch",
            now="2026-05-31T00:00:00Z",
        )
        _seed_project_workdir(conn, scope_id, tmp_path)
        session = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="claude",
            agent_name="worker",
        )
        row = _reserve_submission(
            conn,
            scope_id=scope_id,
            session_id=session["id"],
            author="harness",
            source="harness",
            message_type="harness",
            text="Do not dispatch after archive.",
        )
    with engine.begin() as conn:
        workbench_sessions_service.archive_session(conn, session["id"])

    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post(
                "/internal/dispatch_async",
                json={
                    "session_id": session["id"],
                    "text": "Do not dispatch after archive.",
                    "user_message_id": row["id"],
                },
            )

    response = asyncio.run(_go())

    assert response.status_code == 409
    assert response.json()["code"] == "session_archived"
    controller.message_handler.handle_user_message.assert_not_awaited()


def test_dispatch_async_archive_after_start_does_not_reclaim_may_have_written_delivery(
    monkeypatch,
    tmp_path,
):
    from core.services import sessions as sessions_service
    from storage import messages_service, workbench_sessions_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_archive_during_submit",
            now="2026-05-31T00:00:00Z",
        )
        _seed_project_workdir(conn, scope_id, tmp_path)
        session = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="claude",
            agent_name="worker",
        )
        row = _reserve_submission(
            conn,
            scope_id=scope_id,
            session_id=session["id"],
            author="harness",
            source="harness",
            message_type="harness",
            text="Archive before acceptance.",
        )

    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    real_submit = controller.session_turns.submit

    async def submit_then_archive(*args, **kwargs):
        submission = await real_submit(*args, **kwargs)
        with engine.begin() as conn:
            workbench_sessions_service.archive_session(conn, session["id"])
        return submission

    monkeypatch.setattr(controller.session_turns, "submit", submit_then_archive)
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/internal/dispatch_async",
                json={
                    "session_id": session["id"],
                    "text": "Archive before acceptance.",
                    "user_message_id": row["id"],
                },
            )
        for _ in range(100):
            if session["id"] not in app.state.in_flight_dispatches:
                break
            await asyncio.sleep(0.01)
        return response

    response = asyncio.run(_go())

    assert response.status_code == 202
    controller.command_handler.handle_stop.assert_not_awaited()
    with engine.connect() as conn:
        delivery = message_deliveries.get_delivery(conn, row["id"])
    assert delivery is not None
    assert delivery["state"] not in {"retired", "queued", "reserved"}


def test_slow_live_show_post_cli_timeout_waits_without_duplicate_submit(
    monkeypatch,
    tmp_path,
    capsys,
):
    import json
    import threading

    from core.services import sessions as sessions_service
    from core.show_session_events import ShowSessionEventStore
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope
    from vibe import cli, internal_client, ui_server

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_show_cli_timeout",
            now="2026-05-31T00:00:00Z",
        )
        _seed_project_workdir(conn, scope_id, tmp_path)
        session = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="claude",
            agent_name="worker",
        )

    turn_started = threading.Event()

    async def handler(_context, _text):
        turn_started.set()

    controller = _build_controller_double(handler)
    internal_app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=internal_app)
    manager = controller.session_turns
    real_submit = manager.submit
    submissions = []

    async def tracked_submit(*args, **kwargs):
        submissions.append((args, kwargs))
        return await real_submit(*args, **kwargs)

    monkeypatch.setattr(manager, "submit", tracked_submit)
    dispatch_entered = threading.Event()
    release_dispatch = threading.Event()
    request_thread: threading.Thread | None = None
    request_errors: list[BaseException] = []

    async def slow_dispatch(payload, **_kwargs):
        dispatch_entered.set()
        assert await asyncio.to_thread(release_dispatch.wait, 2)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.post("/internal/dispatch_async", json=payload)
        return {"status_code": response.status_code, "body": response.json()}

    monkeypatch.setattr(internal_client, "dispatch_async", slow_dispatch)

    def timeout_after_live_request(request, **_kwargs):
        nonlocal request_thread
        posted = json.loads(request.data.decode("utf-8"))

        def run_live_request():
            try:
                store = ShowSessionEventStore()
                try:
                    event = store.append(
                        session["id"],
                        posted,
                        reserve_dispatch=True,
                    )
                finally:
                    store.close()
                asyncio.run(ui_server._run_show_event_dispatch(event))
            except BaseException as exc:  # pragma: no cover - thread relay
                request_errors.append(exc)

        request_thread = threading.Thread(target=run_live_request)
        request_thread.start()
        assert dispatch_entered.wait(1)
        threading.Timer(0.1, release_dispatch.set).start()
        raise TimeoutError("CLI deadline elapsed")

    monkeypatch.setattr(cli.urllib.request, "urlopen", timeout_after_live_request)
    monkeypatch.setattr(
        cli,
        "_local_show_events_url",
        lambda session_id: f"http://127.0.0.1:5123/api/show/sessions/{session_id}/events",
    )
    monkeypatch.setattr(
        ui_server,
        "record_local_show_event",
        lambda *args, **kwargs: pytest.fail("ambiguous timeout must not use local fallback"),
    )
    event_input = {
        "id": "show_evt_slow_live_cli",
        "type": "human.annotation.created",
        "annotation": {
            "intent": "comment",
            "comment": "Wait for the original live request.",
            "dispatch": True,
        },
    }
    args = cli.build_parser().parse_args(
        [
            "show",
            "event",
            "--session-id",
            session["id"],
            "--event-json",
            json.dumps(event_input),
            "--json",
        ]
    )

    assert cli.cmd_show(args) == 0
    assert request_thread is not None
    request_thread.join(2)
    assert not request_thread.is_alive()
    assert request_errors == []
    assert len(submissions) == 1
    assert turn_started.wait(1)
    controller.message_handler.handle_user_message.assert_awaited_once()
    assert json.loads(capsys.readouterr().out)["event_id"] == event_input["id"]


@pytest.mark.parametrize(
    ("comment", "anchor", "expected_display"),
    [
        (
            "Deliver after the active turn.",
            None,
            {"direction": "user", "action": "created"},
        ),
        (
            "",
            {"text": "Busy annotation quote"},
            {
                "direction": "user",
                "action": "created",
                "quote": "Busy annotation quote",
            },
        ),
    ],
)
def test_dispatch_async_defers_show_annotation_and_runs_it_after_active_turn(
    monkeypatch,
    tmp_path,
    comment,
    anchor,
    expected_display,
):
    from core.services import sessions as sessions_service
    from core.show_session_events import ShowSessionEventStore
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_show_queue",
            now="2026-05-31T00:00:00Z",
        )
        _seed_project_workdir(conn, scope_id, tmp_path)
        session = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="claude",
            agent_name="worker",
        )
        first_delivery = _reserve_submission(
            conn,
            scope_id=scope_id,
            session_id=session["id"],
            text="active turn",
        )
    session_id = session["id"]

    first_started = asyncio.Event()
    release_first = asyncio.Event()
    seen_texts: list[str] = []

    async def handler(ctx, text):
        seen_texts.append(text)
        if text == "active turn":
            first_started.set()
            await release_first.wait()
        controller.mark_turn_complete(ctx)

    controller = _build_controller_double(handler=handler)
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            first = await client.post(
                "/internal/dispatch_async",
                json={
                    "session_id": session_id,
                    "text": "active turn",
                    "user_message_id": first_delivery["id"],
                },
            )
            assert first.status_code == 202
            await asyncio.wait_for(first_started.wait(), timeout=3)

            store = ShowSessionEventStore()
            try:
                annotation = store.append(
                    session_id,
                    {
                        "type": "human.annotation.created",
                        "annotation": {
                            "intent": "comment",
                            "comment": comment,
                            "dispatch": True,
                            **({"anchor": anchor} if anchor else {}),
                        },
                    },
                    reserve_dispatch=True,
                )
            finally:
                store.close()
            assert annotation["message"] is None
            assert annotation["delivery"]["state"] == "reserved"
            enriched_dispatch_text = annotation["delivery"]["dispatch_text"]
            assert f"Show event id: {annotation['id']}" in enriched_dispatch_text
            queued = await client.post(
                "/internal/dispatch_async",
                json={
                    "session_id": session_id,
                    "text": enriched_dispatch_text,
                    "user_message_id": annotation["delivery_id"],
                    "show_event_id": annotation["id"],
                },
            )
            assert queued.status_code == 202
            assert queued.json()["queued"] is True
            with engine.connect() as conn:
                pending = message_deliveries.get_delivery(
                    conn,
                    annotation["delivery_id"],
                )
                assert pending is not None
                assert pending["priority"] == "p1"
                assert pending["state"] == "pending_steer"
                payload = message_deliveries.delivery_payload(pending)
                assert payload["text"] == annotation["transcript_text"]
                assert pending["dispatch_text"] == enriched_dispatch_text
                visible = messages_service.list_session_messages(
                    conn,
                    session_id=session_id,
                    limit=50,
                    types=messages_service.TRANSCRIPT_TYPES,
                    tail=True,
                )
            assert visible["messages"] == []
            assert seen_texts == ["active turn"]

            release_first.set()
            for _ in range(200):
                if len(seen_texts) == 2 and session_id not in app.state.in_flight_dispatches:
                    break
                await asyncio.sleep(0.02)
            return annotation, enriched_dispatch_text

    annotation, enriched_dispatch_text = asyncio.run(_go())
    assert seen_texts == ["active turn", enriched_dispatch_text]
    with engine.connect() as conn:
        from storage.models import show_session_events

        assert message_deliveries.list_queued(conn, session_id) == []
        visible = messages_service.list_session_messages(
            conn,
            session_id=session_id,
            limit=50,
            types=messages_service.TRANSCRIPT_TYPES,
            tail=True,
        )
        linked_message_id = conn.execute(
            select(show_session_events.c.message_id).where(show_session_events.c.id == annotation["id"])
        ).scalar_one()
    assert [message["text"] for message in visible["messages"]] == [
        "active turn",
        annotation["transcript_text"],
    ]
    delivered = visible["messages"][-1]
    assert delivered["type"] == messages_service.ANNOTATION_TYPE
    assert delivered["author"] == messages_service.HARNESS_TYPE
    assert delivered["source"] == messages_service.HARNESS_TYPE
    assert delivered["author_name"] == "show_annotation"
    assert delivered["content"]["annotation"] == expected_display
    assert delivered["id"] == annotation["delivery_id"]
    assert delivered["metadata"]["source"] == "show_page"
    assert delivered["metadata"]["show_event_id"] == annotation["id"]
    assert linked_message_id == delivered["id"]


def test_startup_recovered_annotation_retries_reserved_dispatch_text(
    monkeypatch,
    tmp_path,
):
    from core.services import sessions as sessions_service
    from core.show_session_events import ShowSessionEventStore
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope
    from vibe import ui_server

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_show_sweep_flush",
            now="2026-05-31T00:00:00Z",
        )
        _seed_project_workdir(conn, scope_id, tmp_path)
        session = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="claude",
            agent_name="worker",
        )
    store = ShowSessionEventStore()
    try:
        annotation = store.append(
            session["id"],
            {
                "type": "human.annotation.created",
                "annotation": {
                    "intent": "comment",
                    "comment": "Recover and deliver this.",
                    "dispatch": True,
                    "anchor": {
                        "kind": "element",
                        "selector": "#summary",
                        "text": "Quarterly summary",
                    },
                },
            },
            reserve_dispatch=True,
        )
    finally:
        store.close()
    expected_dispatch = annotation["delivery"]["dispatch_text"]

    controller = _build_controller_double()
    internal_app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=internal_app)
    manager = controller.session_turns
    manager._build_context = lambda sid: MessageContext(
        user_id="U",
        channel_id="C",
        platform="avibe",
        platform_specific={"agent_session_id": sid},
    )
    dispatched = []

    async def capture_run(
        sid,
        context,
        text,
        *,
        source=SOURCE_HUMAN,
        logical_turn_id=None,
        **_kwargs,
    ):
        dispatched.append((sid, context.message_id, text, source))
        _bind_test_native_start(engine, context)
        manager._terminalize_durable_turn(
            logical_turn_id,
            "completed",
            settled_by=SETTLED_BY_TERMINAL_RESULT,
            evidence_kind="test_terminal",
        )

    manager._run = capture_run

    async def dispatch_through_controller(payload, **_kwargs):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.post("/internal/dispatch_async", json=payload)
            await asyncio.sleep(0)
        return {"status_code": response.status_code, "body": response.json()}

    monkeypatch.setattr(
        "vibe.internal_client.dispatch_async",
        dispatch_through_controller,
    )
    outcome = asyncio.run(ui_server._run_show_event_dispatch(annotation))
    assert outcome == ui_server._ShowEventDispatchOutcome.ACCEPTED

    with engine.connect() as conn:
        queued = message_deliveries.list_queued(conn, session["id"])
        transcript = messages_service.list_session_messages(
            conn,
            session_id=session["id"],
            limit=50,
            types=messages_service.TRANSCRIPT_TYPES,
            tail=True,
        )["messages"]
    assert queued == []
    assert dispatched[0][1] == annotation["delivery_id"]
    assert dispatched[0][2] == expected_dispatch
    assert "Anchor: #summary" in dispatched[0][2]
    assert f"Show event id: {annotation['id']}" in dispatched[0][2]
    assert transcript[0]["type"] == messages_service.ANNOTATION_TYPE
    assert transcript[0]["text"] == "Recover and deliver this."
    assert transcript[0]["content"]["annotation"]["quote"] == "Quarterly summary"


def test_idle_show_p1_admission_starts_before_recovery_drain(
    monkeypatch,
    tmp_path,
):
    from core.services import sessions as sessions_service
    from core.show_session_events import ShowSessionEventStore
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import show_session_events
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_show_idle_queue",
            now="2026-05-31T00:00:00Z",
        )
        _seed_project_workdir(conn, scope_id, tmp_path)
        session = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="claude",
            agent_name="worker",
        )
        older = message_deliveries.enqueue_queued(
            conn,
            scope_id=scope_id,
            session_id=session["id"],
            text="older queued message",
        )
    store = ShowSessionEventStore()
    try:
        annotation = store.append(
            session["id"],
            {
                "type": "human.annotation.created",
                "annotation": {
                    "intent": "comment",
                    "comment": "Join and drain.",
                    "dispatch": True,
                },
            },
            reserve_dispatch=True,
        )
    finally:
        store.close()

    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    manager = controller.session_turns
    manager._build_context = lambda sid: MessageContext(
        user_id="U",
        channel_id="C",
        platform="avibe",
        platform_specific={"agent_session_id": sid},
    )
    runs = []

    async def capture_run(
        sid,
        context,
        text,
        *,
        source=SOURCE_HUMAN,
        logical_turn_id=None,
        **_kwargs,
    ):
        runs.append((sid, text, source))
        _bind_test_native_start(engine, context)

    manager._run = capture_run
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post(
                "/internal/dispatch_async",
                json={
                    "session_id": session["id"],
                    "text": annotation["delivery"]["dispatch_text"],
                    "user_message_id": annotation["delivery_id"],
                    "show_event_id": annotation["id"],
                },
            )

    response = asyncio.run(_go())

    assert response.status_code == 202
    assert response.json()["queued"] is False
    assert response.json().get("drained") is None
    assert len(runs) == 1
    assert runs[0][1] == annotation["delivery"]["dispatch_text"]
    with engine.connect() as conn:
        queued = message_deliveries.list_queued(conn, session["id"])
        linked_message_id = conn.execute(
            select(show_session_events.c.message_id).where(
                show_session_events.c.id == annotation["id"]
            )
        ).scalar_one()
        visible = messages_service.list_session_messages(
            conn,
            session_id=session["id"],
            limit=50,
            types=messages_service.TRANSCRIPT_TYPES,
            tail=True,
        )["messages"]
    assert [row["id"] for row in queued] == [older["id"]]
    assert linked_message_id == annotation["delivery_id"]
    assert [row["text"] for row in visible] == [annotation["transcript_text"]]
    assert visible[0]["id"] == annotation["delivery_id"]
    assert visible[0]["type"] == messages_service.ANNOTATION_TYPE
    assert visible[0]["author_name"] == "show_annotation"
    assert visible[0]["native_message_id"] == f"show:{annotation['id']}"
    assert asyncio.run(manager.flush_queue(session["id"])) is False


def test_flush_promoted_user_row_uses_fresh_id_for_same_second_order(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr(
        message_deliveries,
        "new_delivery_id",
        lambda: "msg_000000000000200aaaaaaaa",
    )
    session_id = _seed_avibe_session_with_queue([("queued follow-up", None)])

    from sqlalchemy import update

    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.models import message_deliveries as delivery_rows, messages

    with create_sqlite_engine().begin() as conn:
        queued = message_deliveries.list_queued(conn, session_id)[0]
        queued_id = queued["id"]
        conn.execute(
            update(delivery_rows)
            .where(delivery_rows.c.id == queued["id"])
            .values(submitted_at="2026-06-22T00:00:37.000000Z")
        )
        generated_ids = iter(
            (
                "msg_000000000000100aaaaaaaa",
                "msg_000000000000300aaaaaaaa",
            )
        )
        monkeypatch.setattr(messages_service, "_new_message_id", lambda: next(generated_ids))
        monkeypatch.setattr(messages_service, "_utc_now_iso", lambda: "2026-06-22T00:00:37Z")
        monkeypatch.setattr(
            message_deliveries,
            "turn_now_iso",
            lambda: "2026-06-22T00:00:37.000000Z",
        )
        result = messages_service.append(
            conn,
            scope_id=queued["scope_id"],
            session_id=session_id,
            platform="avibe",
            author="agent",
            message_type="result",
            text="preceding result",
        )
        conn.execute(
            update(messages)
            .where(messages.c.id == result["id"])
            .values(
                created_at="2026-06-22T00:00:37.000000Z",
                updated_at="2026-06-22T00:00:37.000000Z",
            )
        )

    manager, _runs = _manager_capturing_runs()
    from core.inbox_events import bus

    published = []
    monkeypatch.setattr(
        bus,
        "publish",
        lambda event_type, data: published.append((event_type, data)),
    )

    async def append_fast_result(
        sid,
        context,
        text,
        *,
        source=SOURCE_HUMAN,
        logical_turn_id=None,
        delivery_id=None,
        **_kwargs,
    ):
        _bind_test_native_start(create_sqlite_engine(), context)
        with create_sqlite_engine().begin() as conn:
            accepted = message_deliveries.materialize_start_acceptance(
                conn,
                turn_id=logical_turn_id,
                evidence={"kind": "test_native_acceptance"},
            )
            assert accepted
            messages_service.append(
                conn,
                scope_id=context.platform_specific.get("scope_id"),
                session_id=sid,
                platform="avibe",
                author="agent",
                message_type="result",
                text="fast queued result",
            )
        manager._publish_materialized_delivery(delivery_id)

    async def fail_if_sleeping(_delay):
        pytest.fail("queue promotion must not wait for wall-clock time")

    monkeypatch.setattr(session_turns.asyncio, "sleep", fail_if_sleeping)
    manager._run = append_fast_result
    assert asyncio.run(manager.flush_queue(session_id)) is True

    with create_sqlite_engine().connect() as conn:
        transcript = messages_service.list_session_messages(
            conn,
            session_id=session_id,
            types=("user", "result"),
        )
    assert [(row["type"], row["text"]) for row in transcript["messages"]] == [
        ("result", "preceding result"),
        ("user", "queued follow-up"),
        ("result", "fast queued result"),
    ]
    assert [row["created_at"] for row in transcript["messages"]] == [
        "2026-06-22T00:00:37.000000Z",
        "2026-06-22T00:00:37.000000Z",
        "2026-06-22T00:00:37.000000Z",
    ]
    assert [row["id"] for row in transcript["messages"]] == [
        "msg_000000000000100aaaaaaaa",
        "msg_000000000000200aaaaaaaa",
        "msg_000000000000300aaaaaaaa",
    ]
    assert queued_id == "msg_000000000000200aaaaaaaa"
    assert [
        (event_type, data.get("id"))
        for event_type, data in published
        if event_type == "message.new"
    ] == [("message.new", "msg_000000000000200aaaaaaaa")]
    assert not hasattr(session_turns, "_timestamp_after_latest_session_message")
    assert not hasattr(session_turns, "_wait_until_message_timestamp")


def test_flush_materializes_exact_head_and_links_delivery_dependencies(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    session_id = _seed_avibe_session_with_queue(
        [
            ("canonical queued message", None),
            ("merged queued message", None),
        ]
    )

    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.models import show_session_events

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        queued = message_deliveries.list_queued(conn, session_id)
        head_id = queued[0]["id"]
        second_id = queued[1]["id"]
        for index, delivery_id in enumerate((head_id, second_id), start=1):
            conn.execute(
                show_session_events.insert().values(
                    id=f"show_evt_repoint_{index}",
                    session_id=session_id,
                    event_type="human.annotation.created",
                    actor="human",
                    scope="page",
                    anchor_json="{}",
                    payload_json="{}",
                    transcript_text=f"annotation {index}",
                    message_id=None,
                    delivery_id=delivery_id,
                    created_at=f"2026-06-22T00:00:0{index}Z",
                )
            )

    manager, _runs = _manager_accepting_runs()
    assert asyncio.run(manager.flush_queue(session_id)) is True

    with engine.connect() as conn:
        visible = messages_service.list_session_messages(
            conn,
            session_id=session_id,
            types=("user",),
        )["messages"]
        event_message_ids = conn.execute(
            select(show_session_events.c.message_id).order_by(
                show_session_events.c.id
            )
        ).scalars().all()
        queued_ids = [row["id"] for row in message_deliveries.list_queued(conn, session_id)]

    assert len(visible) == 1
    assert visible[0]["id"] == head_id
    assert event_message_ids == [head_id, head_id]
    assert queued_ids == []


def test_flush_claims_compatible_user_rows_as_one_turn_and_one_message(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    session_id = _seed_avibe_session_with_queue(
        [
            ("first show annotation", None),
            ("second show annotation", None),
        ]
    )

    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.models import show_session_events

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        queued = message_deliveries.list_queued(conn, session_id)
        for index, row in enumerate(queued, start=1):
            event_id = f"show_evt_identity_{index}"
            conn.execute(
                show_session_events.insert().values(
                    id=event_id,
                    session_id=session_id,
                    event_type="human.annotation.created",
                    actor="human",
                    scope="default",
                    anchor_json="{}",
                    payload_json="{}",
                    transcript_text=row["text"],
                    message_id=None,
                    delivery_id=row["id"],
                    created_at=f"2026-06-22T00:00:0{index}Z",
                )
            )

    manager, runs = _manager_accepting_runs()
    assert asyncio.run(manager.flush_queue(session_id)) is True

    with engine.connect() as conn:
        visible = messages_service.list_session_messages(
            conn,
            session_id=session_id,
            types=("user",),
        )["messages"]
        event_links = conn.execute(
            select(show_session_events.c.id, show_session_events.c.message_id).order_by(
                show_session_events.c.id
            )
        ).all()

    assert [text for text, _source, _context in runs] == [
        "first show annotation\nsecond show annotation"
    ]
    assert len(visible) == 1
    assert event_links == [
        ("show_evt_identity_1", visible[0]["id"]),
        ("show_evt_identity_2", visible[0]["id"]),
    ]


def test_async_dispatch_flushes_one_compatible_queue_segment_on_turn_end(monkeypatch, tmp_path):
    """Natural settlement combines the compatible FIFO segment into one Turn."""
    from core.services import sessions as sessions_service
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn, platform="avibe", scope_type="project", native_id="proj_flush", now="2026-05-31T00:00:00Z"
        )
        _seed_project_workdir(conn, scope_id, tmp_path)
        session = sessions_service.create_session(
            conn, scope_id=scope_id, agent_backend="claude", agent_name="worker"
        )
        first = _reserve_submission(
            conn,
            scope_id=scope_id,
            session_id=session["id"],
            text="first turn",
        )
    session_id = session["id"]

    seen_texts: list[str] = []

    async def handler(ctx, text):
        seen_texts.append(text)
        # Simulate the user queueing two messages WHILE the first turn runs (the
        # real flow — queued rows only exist during an active turn).
        if text == "first turn":
            with engine.begin() as conn:
                message_deliveries.enqueue_queued(
                    conn,
                    scope_id=scope_id,
                    session_id=session_id,
                    platform="avibe",
                    author="user",
                    source="user",
                    text="q1",
                    author_id="remote:user-a",
                    metadata={"_web_push_user_key": "remote:user-a"},
                )
                message_deliveries.enqueue_queued(
                    conn,
                    scope_id=scope_id,
                    session_id=session_id,
                    platform="avibe",
                    author="user",
                    source="user",
                    text="q2",
                    author_id="remote:user-a",
                )
        controller.mark_turn_complete(ctx)  # release each turn immediately
        return None

    controller = _build_controller_double(handler=handler)
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await client.post(
                "/internal/dispatch_async",
                json={
                    "session_id": session_id,
                    "text": "first turn",
                    "user_message_id": first["id"],
                },
            )
        # Wait for the first turn AND the flush turn to both drain the queue.
        for _ in range(200):
            if len(seen_texts) >= 2 and session_id not in app.state.in_flight_dispatches:
                break
            await asyncio.sleep(0.02)

    asyncio.run(_go())
    assert seen_texts == ["first turn", "q1\nq2"]
    with engine.connect() as conn:
        assert message_deliveries.list_queued(conn, session_id) == []
        transcript = messages_service.list_session_messages(
            conn,
            session_id=session_id,
            types=("user",),
            include_private_metadata=True,
        )
    assert [m["text"] for m in transcript["messages"]] == ["first turn", "q1\nq2"]
    assert transcript["messages"][1]["author_id"] == "remote:user-a"
    assert transcript["messages"][1]["metadata"]["_web_push_user_key"] == "remote:user-a"


def test_cancel_resumes_the_oldest_queued_segment(monkeypatch, tmp_path):
    """Once Stop makes the Session idle, its queued work starts immediately."""
    from core.services import sessions as sessions_service
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn, platform="avibe", scope_type="project", native_id="proj_noflush", now="2026-05-31T00:00:00Z"
        )
        _seed_project_workdir(conn, scope_id, tmp_path)
        session = sessions_service.create_session(
            conn, scope_id=scope_id, agent_backend="claude", agent_name="worker"
        )
        first = _reserve_submission(
            conn,
            scope_id=scope_id,
            session_id=session["id"],
            text="first",
        )
    session_id = session["id"]

    started = asyncio.Event()
    seen: list[str] = []

    async def long_handler(ctx, text):
        _bind_test_native_start(engine, ctx)
        seen.append(text)
        if text == "first":
            started.set()
            await asyncio.sleep(5)  # held until the test cancels it
        else:
            controller.mark_turn_complete(ctx)
        return TurnDispatchOutcome(error=None, settled_by=SETTLED_BY_TERMINAL_RESULT)

    controller = _build_controller_double(handler=long_handler)
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await client.post(
                "/internal/dispatch_async",
                json={
                    "session_id": session_id,
                    "text": "first",
                    "user_message_id": first["id"],
                },
            )
            await asyncio.wait_for(started.wait(), timeout=3)
            # Queue a message while the turn runs, then Stop.
            with engine.begin() as conn:
                message_deliveries.enqueue_queued(conn, scope_id=scope_id, session_id=session_id, text="q1")
            await client.post(f"/internal/cancel/{session_id}")
            for _ in range(300):
                if seen == ["first", "q1"] and session_id not in app.state.in_flight_dispatches:
                    break
                await asyncio.sleep(0.02)

    asyncio.run(_go())
    with engine.connect() as conn:
        queued = message_deliveries.list_queued(conn, session_id)
        transcript = messages_service.list_session_messages(conn, session_id=session_id, types=("user",))
    assert seen == ["first", "q1"]
    assert queued == []
    assert [row["text"] for row in transcript["messages"]] == ["first", "q1"]


def test_turn_state_reflects_in_flight():
    """``/internal/turn-state`` reports whether a turn is running, so a freshly
    loaded / reconnected Chat page can restore its Stop state."""
    controller = _build_controller_double()
    controller.agent_service.runtime_turn_started.return_value = True
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            idle = (await client.get("/internal/turn-state/ses_ts")).json()
            # Simulate an in-flight turn.
            task = asyncio.create_task(asyncio.sleep(60))
            app.state.in_flight_dispatches["ses_ts"] = session_turns.Turn(
                task=task,
                context=MessageContext(
                    user_id="U",
                    channel_id="C",
                    platform="avibe",
                    platform_specific={"agent_session_target": {"agent_backend": "opencode"}},
                ),
            )
            busy = (await client.get("/internal/turn-state/ses_ts")).json()
            task.cancel()
            return idle, busy

    idle, busy = asyncio.run(_go())
    assert idle["in_flight"] is False
    assert busy["in_flight"] is True
    assert busy["native_turn_started"] is True
    assert busy["backend"] == "opencode"


def test_turn_state_projects_a_restored_durable_owner_without_in_flight(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _engine, session, turn_id = _create_active_test_turn(
        tmp_path,
        native_id="restored-turn-state",
        backend="opencode",
    )
    app = internal_server.create_app(_build_controller_double())
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return (await client.get(f"/internal/turn-state/{session['id']}")).json()

    state = asyncio.run(_go())
    assert state["in_flight"] is True
    assert state["foreground"] == "running"
    assert state["native_turn_started"] is True
    assert state["backend"] == "opencode"
    assert state["owner"]["runtime_key"] == f"runtime:{session['id']}"
    assert state["owner"]["native_turn_started"] is True
    assert turn_id


def test_cancel_returns_404_when_session_not_in_flight(tmp_path, monkeypatch):
    from storage.importer import ensure_sqlite_state

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    app = internal_server.create_app(_build_controller_double())
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post("/internal/cancel/ses_unknown")

    resp = asyncio.run(_go())
    assert resp.status_code == 404
    body = resp.json()
    assert body["ok"] is False
    assert body["code"] == "not_in_flight"


def test_cancel_rejects_a_blank_explicit_run_id(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session, turn_id = _create_active_test_turn(
        tmp_path,
        native_id="proj_blank_run_cancel",
    )
    session_id = session["id"]
    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post(
                f"/internal/cancel/{session_id}",
                params={"run_id": "   "},
            )

    response = asyncio.run(_go())

    assert response.status_code == 400
    assert response.json() == {
        "ok": False,
        "code": "invalid_run_id",
        "session_id": session_id,
        "reason": "run_id_required",
    }
    controller.command_handler.handle_stop.assert_not_awaited()
    with engine.connect() as conn:
        turn = message_deliveries.get_turn(conn, turn_id)
    assert turn is not None
    assert turn["state"] == "active"
    assert turn["control_state"] is None


def test_cancel_releases_stale_turn_when_backend_not_active(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session, turn_id = _create_active_test_turn(
        tmp_path,
        native_id="proj_stale_cancel",
    )
    session_id = session["id"]
    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)
    statuses = []
    notices = []
    controller.set_agent_status = lambda session_id, status: statuses.append((session_id, status))

    async def _go():
        task = asyncio.create_task(asyncio.sleep(60))
        context = MessageContext(
            user_id="U",
            channel_id="C",
            platform="avibe",
            platform_specific={"agent_session_id": session_id},
        )
        app.state.in_flight_dispatches[session_id] = session_turns.Turn(
            task=task,
            context=context,
            logical_turn_id=turn_id,
        )

        async def _not_active(_context):
            notices.append(_context.platform_specific.get("suppress_stop_no_active_notice"))
            _context.platform_specific["stop_failure_reason"] = "not_active"
            return False

        controller.command_handler.handle_stop = AsyncMock(side_effect=_not_active)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(f"/internal/cancel/{session_id}")
        for _ in range(200):
            if session_id not in app.state.in_flight_dispatches:
                break
            await asyncio.sleep(0.02)
        return resp, task

    resp, task = asyncio.run(_go())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "stale_released"
    assert body["reason"] == "not_active"
    assert task.cancelled()
    assert session_id not in app.state.in_flight_dispatches
    assert statuses == []
    from storage.models import agent_sessions

    with engine.connect() as conn:
        status = conn.execute(
            select(agent_sessions.c.agent_status).where(agent_sessions.c.id == session_id)
        ).scalar_one()
    assert status == "idle"
    assert notices == [True]


def test_cancel_accepts_terminal_settlement_that_races_the_stop_receipt(
    tmp_path,
    monkeypatch,
):
    """HFR-431: a successful Stop may emit terminal proof before its receipt lands."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _engine, session, turn_id = _create_active_test_turn(
        tmp_path,
        native_id="proj_terminal_stop_race",
    )
    session_id = session["id"]
    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)

    async def _go():
        task = asyncio.create_task(asyncio.sleep(60))
        context = MessageContext(
            user_id="U",
            channel_id="C",
            platform="avibe",
            platform_specific={"agent_session_id": session_id},
        )
        app.state.in_flight_dispatches[session_id] = session_turns.Turn(
            task=task,
            context=context,
            logical_turn_id=turn_id,
        )

        async def _stop_and_settle(_context):
            terminal = controller.session_turns._terminalize_durable_turn(
                turn_id,
                "canceled",
                settled_by=SETTLED_BY_STOPPED,
                evidence_kind="test_stop_terminal",
            )
            assert terminal["changed"] is True
            return True

        controller.command_handler.handle_stop = AsyncMock(
            side_effect=_stop_and_settle
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.post(f"/internal/cancel/{session_id}")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return response

    response = asyncio.run(_go())
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "session_id": session_id,
        "status": "stale_released",
        "reason": "already_terminal",
    }


def test_cancel_waits_for_stale_dispatch_cleanup_before_releasing(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _engine, session, turn_id = _create_active_test_turn(
        tmp_path,
        native_id="proj_stale_cleanup",
    )
    session_id = session["id"]
    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)
    controller.set_agent_status = lambda session_id, status: None

    async def _go():
        done_event = asyncio.Event()
        cleanup_started = asyncio.Event()
        allow_cleanup = asyncio.Event()
        context = MessageContext(
            user_id="U",
            channel_id="C",
            platform="avibe",
            platform_specific={"agent_session_id": session_id},
        )

        async def _stale_dispatch():
            controller.register_turn_sink(
                controller._get_session_key(context),
                on_chunk=AsyncMock(),
                done_event=done_event,
                turn_token="old-turn",
            )
            try:
                await asyncio.sleep(60)
            finally:
                cleanup_started.set()
                await allow_cleanup.wait()
                controller.pop_turn_sink(controller._get_session_key(context), done_event)

        task = asyncio.create_task(_stale_dispatch())
        for _ in range(200):
            if controller.get_turn_sink("avibe::C") is not None:
                break
            await asyncio.sleep(0.01)
        assert controller.get_turn_sink("avibe::C") is not None
        app.state.in_flight_dispatches[session_id] = session_turns.Turn(
            task=task,
            context=context,
            logical_turn_id=turn_id,
        )

        async def _not_active(_context):
            _context.platform_specific["stop_failure_reason"] = "not_active"
            return False

        controller.command_handler.handle_stop = AsyncMock(side_effect=_not_active)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            cancel_task = asyncio.create_task(client.post(f"/internal/cancel/{session_id}"))
            await asyncio.wait_for(cleanup_started.wait(), timeout=1)
            await asyncio.sleep(0.02)
            assert not cancel_task.done()
            assert session_id in app.state.in_flight_dispatches
            allow_cleanup.set()
            resp = await cancel_task
        return resp, task

    resp, task = asyncio.run(_go())
    assert resp.status_code == 200
    assert resp.json()["status"] == "stale_released"
    assert task.cancelled()
    assert controller.get_turn_sink("avibe::C") is None


def test_cancel_keeps_turn_when_backend_interrupt_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _engine, session, turn_id = _create_active_test_turn(
        tmp_path,
        native_id="proj_failed_stop",
    )
    session_id = session["id"]
    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)

    async def _go():
        task = asyncio.create_task(asyncio.sleep(60))
        context = MessageContext(
            user_id="U",
            channel_id="C",
            platform="avibe",
            platform_specific={"agent_session_id": session_id},
        )
        app.state.in_flight_dispatches[session_id] = session_turns.Turn(
            task=task,
            context=context,
            logical_turn_id=turn_id,
        )

        async def _interrupt_failed(_context):
            _context.platform_specific["stop_failure_reason"] = "interrupt_failed"
            return False

        controller.command_handler.handle_stop = AsyncMock(side_effect=_interrupt_failed)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(f"/internal/cancel/{session_id}")
        held = session_id in app.state.in_flight_dispatches and not task.done()
        task.cancel()
        return resp, held

    resp, held = asyncio.run(_go())
    assert resp.status_code == 409
    body = resp.json()
    assert body["ok"] is False
    assert body["code"] == "stop_unknown"
    assert body["reason"] == "stop_unknown"
    assert held is True


def test_release_for_backend_refresh_cancels_matching_turn_and_sets_idle():
    controller = _build_controller_double()
    manager = session_turns.SessionTurnManager(controller)
    statuses = []
    controller.set_agent_status = lambda session_id, status: statuses.append((session_id, status))

    async def _go():
        async def _busy():
            await asyncio.sleep(60)

        task = asyncio.create_task(_busy())
        ctx = MessageContext(user_id="U", channel_id="ses_codex", platform="avibe")
        ctx.platform_specific = {
            "agent_session_id": "ses_codex",
            "agent_session_target": {"agent_backend": "codex"},
        }
        manager.in_flight["ses_codex"] = session_turns.Turn(task=task, context=ctx)
        manager.begin_backend_drain("codex")

        released = await manager.release_for_backend_refresh(
            backend="codex",
            base_session_ids={"ses_codex"},
        )
        try:
            await task
        except asyncio.CancelledError:
            pass
        return released, task.cancelled()

    released, cancelled = asyncio.run(_go())

    assert released == 1
    assert cancelled is True
    assert statuses == [("ses_codex", "idle")]
    assert manager._deferred_restart_sessions == {"codex": {"ses_codex"}}


def test_release_for_backend_refresh_leaves_other_backend_turn_running():
    controller = _build_controller_double()
    manager = session_turns.SessionTurnManager(controller)
    statuses = []
    controller.set_agent_status = lambda session_id, status: statuses.append((session_id, status))

    async def _go():
        async def _busy():
            await asyncio.sleep(60)

        task = asyncio.create_task(_busy())
        ctx = MessageContext(user_id="U", channel_id="ses_claude", platform="avibe")
        ctx.platform_specific = {
            "agent_session_id": "ses_claude",
            "agent_session_target": {"agent_backend": "claude"},
        }
        manager.in_flight["ses_claude"] = session_turns.Turn(task=task, context=ctx)
        try:
            released = await manager.release_for_backend_refresh(
                backend="codex",
                base_session_ids={"ses_claude"},
            )
            return released, task.done()
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    released, done = asyncio.run(_go())

    assert released == 0
    assert done is False
    assert statuses == []


def test_backend_drain_queues_idle_session_without_dispatching(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session = _create_test_session(
        tmp_path,
        native_id="proj_backend_drain",
        backend="codex",
    )
    session_id = session["id"]
    controller = _build_controller_double()
    manager = session_turns.SessionTurnManager(controller)
    context = MessageContext(user_id="U", channel_id=session_id, platform="avibe")
    context.platform_specific = {
        "agent_session_id": session_id,
        "scope_id": session["scope_id"],
        "agent_session_target": {"agent_backend": "codex"},
    }

    async def _go():
        manager.begin_backend_drain("codex")
        return await manager.submit(session_id, context, "next")

    result = asyncio.run(_go())

    assert result == session_turns.TurnSubmissionResult(
        route="enqueued",
        queue_persisted=True,
    )
    with engine.connect() as conn:
        assert [row["text"] for row in message_deliveries.list_queued(conn, session_id)] == ["next"]
    assert manager.is_in_flight(session_id) is False
    assert manager._deferred_restart_sessions == {"codex": {session_id}}


def test_backend_drain_queues_pre_reserved_idle_submission_before_resume(
    tmp_path,
    monkeypatch,
):
    """A Web-reserved Delivery becomes claimable before the backend drain ends."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session = _create_test_session(
        tmp_path,
        native_id="proj_backend_drain_reserved",
        backend="codex",
    )
    session_id = session["id"]
    with engine.begin() as conn:
        reserved = _reserve_submission(
            conn,
            scope_id=session["scope_id"],
            session_id=session_id,
            text="reserved during refresh",
        )

    controller = _build_controller_double()
    manager = session_turns.SessionTurnManager(controller)
    manager._start_persisted_turn = AsyncMock(return_value=None)
    context = MessageContext(
        user_id="U",
        channel_id=session_id,
        platform="avibe",
        platform_specific={
            "agent_session_id": session_id,
            "scope_id": session["scope_id"],
            "delivery_id": reserved["id"],
            "agent_session_target": {"agent_backend": "codex"},
        },
    )

    async def _go():
        manager.begin_backend_drain("codex")
        admitted = await manager.submit(
            session_id,
            context,
            "reserved during refresh",
        )
        with engine.connect() as conn:
            queued = message_deliveries.get_delivery(conn, reserved["id"])
        await manager.end_backend_drain("codex")
        return admitted, queued

    admitted, queued = asyncio.run(_go())

    assert admitted == session_turns.TurnSubmissionResult(
        route="enqueued",
        queue_persisted=True,
    )
    assert queued is not None and queued["state"] == "queued"
    manager._start_persisted_turn.assert_awaited_once()


def test_backend_drain_exposes_failed_queue_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _engine, session = _create_test_session(
        tmp_path,
        native_id="proj_backend_drain_failure",
        backend="codex",
    )
    session_id = session["id"]
    controller = _build_controller_double()
    manager = session_turns.SessionTurnManager(controller)
    context = MessageContext(user_id="U", channel_id=session_id, platform="avibe")
    context.platform_specific = {
        "agent_session_id": session_id,
        "scope_id": session["scope_id"],
        "agent_session_target": {"agent_backend": "codex"},
    }
    monkeypatch.setattr(
        manager,
        "_insert_delivery",
        Mock(side_effect=RuntimeError("queue persistence failed")),
    )

    async def _go():
        manager.begin_backend_drain("codex")
        return await manager.submit(session_id, context, "next")

    with pytest.raises(RuntimeError, match="queue persistence failed"):
        asyncio.run(_go())
    assert manager.is_in_flight(session_id) is False


def test_backend_drain_resolves_inherited_default_agent_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _engine, session = _create_test_session(
        tmp_path,
        native_id="proj_backend_drain_default",
        backend="codex",
    )
    session_id = session["id"]
    controller = _build_controller_double()
    controller.resolve_agent_for_context = Mock(return_value="codex")
    manager = session_turns.SessionTurnManager(controller)
    context = MessageContext(user_id="U", channel_id=session_id, platform="avibe")
    context.platform_specific = {
        "agent_session_id": session_id,
        "scope_id": session["scope_id"],
        "agent_session_target": {"agent_name": None, "agent_backend": None},
    }

    async def _go():
        manager.begin_backend_drain("codex")
        return await manager.submit(session_id, context, "next")

    assert asyncio.run(_go()) == session_turns.TurnSubmissionResult(
        route="enqueued",
        queue_persisted=True,
    )
    assert manager._deferred_restart_sessions == {"codex": {session_id}}


@pytest.mark.no_sqlite_template
def test_backend_drain_flushes_deferred_session_after_cutover():
    controller = _build_controller_double()
    manager = session_turns.SessionTurnManager(controller)
    manager.flush_queue = AsyncMock(return_value=True)
    manager.begin_backend_drain("codex")
    manager._deferred_restart_sessions["codex"].add("ses_codex")

    asyncio.run(manager.end_backend_drain("codex"))

    manager.flush_queue.assert_awaited_once_with("ses_codex")
    assert "codex" not in manager._draining_backends


def test_backend_drain_blocks_direct_queue_flush(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session = _create_test_session(
        tmp_path,
        native_id="proj_direct_drain",
        backend="codex",
    )
    session_id = session["id"]
    with engine.begin() as conn:
        message_deliveries.enqueue_queued(
            conn,
            scope_id=session["scope_id"],
            session_id=session_id,
            text="queued",
        )
    controller = _build_controller_double()
    context = MessageContext(user_id="U", channel_id=session_id, platform="avibe")
    context.platform_specific = {
        "agent_session_id": session_id,
        "agent_session_target": {"agent_backend": "codex"},
    }
    manager = session_turns.SessionTurnManager(controller, build_context=lambda _session_id: context)
    manager._run = AsyncMock()  # type: ignore[method-assign]
    manager.begin_backend_drain("codex")

    assert asyncio.run(manager.flush_queue(session_id)) is False
    manager._run.assert_not_awaited()
    with engine.connect() as conn:
        assert [row["text"] for row in message_deliveries.list_queued(conn, session_id)] == ["queued"]
    assert manager._deferred_restart_sessions == {"codex": {session_id}}


def test_stop_during_backend_drain_keeps_existing_queue_parked(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session, turn_id = _create_active_test_turn(
        tmp_path,
        native_id="proj_stop_drain",
        backend="codex",
    )
    session_id = session["id"]
    with engine.begin() as conn:
        message_deliveries.enqueue_queued(
            conn,
            scope_id=session["scope_id"],
            session_id=session_id,
            text="queued",
        )
    controller = _build_controller_double()
    manager = session_turns.SessionTurnManager(controller)
    context = MessageContext(user_id="U", channel_id=session_id, platform="avibe")
    context.platform_specific = {
        "agent_session_id": session_id,
        "agent_session_target": {"agent_backend": "codex"},
    }

    async def _go():
        task = asyncio.create_task(asyncio.sleep(60))
        manager.in_flight[session_id] = session_turns.Turn(
            task=task,
            context=context,
            logical_turn_id=turn_id,
        )
        manager.begin_backend_drain("codex")
        manager._deferred_restart_sessions["codex"].add(session_id)
        result = await manager.cancel(session_id)
        held = not task.done()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return result, held

    result, held = asyncio.run(_go())

    assert result["status"] == "cancel_requested"
    assert held is True
    with engine.connect() as conn:
        assert [row["text"] for row in message_deliveries.list_queued(conn, session_id)] == ["queued"]
    assert manager._deferred_restart_sessions == {"codex": {session_id}}


# ---------------------------------------------------------------------
# Dispatcher hook contract
# ---------------------------------------------------------------------


def test_dispatch_turn_registers_sink_for_dispatcher_hook():
    """Locks the contract between ``dispatch_turn`` and the dispatcher's
    ``_stream_chunk`` helper: the streaming ``on_chunk`` is registered as a
    per-session turn sink (resolvable by session key while the turn runs)
    and cleaned up afterward — not stashed on the per-turn context.
    """

    async def on_chunk(envelope):
        pass

    seen: dict = {}

    async def capture(ctx, text):
        sink = controller.get_turn_sink(controller._get_session_key(ctx))
        seen["on_chunk"] = sink["on_chunk"] if sink else None
        # Release the dispatch the way a real result emit would.
        if sink:
            sink["done_event"].set()

    controller = _build_controller_double(handler=capture)
    ctx = MessageContext(user_id="U", channel_id="C", platform="avibe")
    asyncio.run(dispatch_turn(controller, ctx, "ping", on_chunk=on_chunk))
    assert seen["on_chunk"] is on_chunk
    assert controller.get_turn_sink("avibe::C") is None, "sink cleaned up after the turn"


# ---------------------------------------------------------------------
# Scheduled / watch turn gate (controller.session_turn_gate)
# ---------------------------------------------------------------------


def test_scheduled_gate_idle_runs_turn_with_lifecycle(monkeypatch, tmp_path):
    """An IDLE scheduled run goes through ``_run_turn`` like a Chat turn: it
    registers ``in_flight`` + publishes ``turn.start`` / ``turn.end`` on the bus
    (so the Chat page shows the working indicator + Stop works) and calls
    ``dispatch_turn`` with ``source=SOURCE_SCHEDULED`` and the no-op chunk sink —
    NOT ``on_chunk=None``. The sink isn't about the browser (chunks are discarded;
    avibe renders from ``message.new``); it makes ``dispatch_turn`` HOLD the turn
    open until the backend's terminal result, which keeps ``in_flight`` populated
    for the scheduled turn's whole lifetime so a Chat send can't preempt a
    still-running scheduled turn (Codex P2)."""
    from core import inbox_events
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _engine, session = _create_test_session(
        tmp_path,
        native_id="proj_scheduled_idle",
    )
    session_id = session["id"]

    captured: dict = {}
    started = asyncio.Event()

    async def _fake_dispatch_turn(
        ctrl,
        ctx,
        text,
        *,
        source=SOURCE_HUMAN,
        on_chunk=None,
        lifecycle_snapshot=None,
    ):
        captured["lifecycle_snapshot"] = lifecycle_snapshot
        captured["source"] = source
        captured["on_chunk"] = on_chunk
        captured["text"] = text
        captured["in_flight_while_running"] = session_id in app.state.in_flight_dispatches
        started.set()
        # The manager now branches on the outcome to settle a run whose turn ended
        # without a terminal result, so a double has to return one.
        return TurnDispatchOutcome(error=None, settled_by=SETTLED_BY_TERMINAL_RESULT)

    monkeypatch.setattr(session_turns, "dispatch_turn_with_outcome", _fake_dispatch_turn)

    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    ctx = MessageContext(
        user_id="workbench",
        channel_id=session_id,
        platform="avibe",
        platform_specific={
            "agent_session_id": session_id,
            "agent_session_target": {"agent_backend": "claude"},
        },
    )

    async def _go():
        sub_id, queue = inbox_events.bus.subscribe()
        events: list[str] = []
        try:
            await controller.session_turn_gate.submit_scheduled(session_id, ctx, "digest please")
            await asyncio.wait_for(started.wait(), timeout=3)
            for _ in range(100):
                if session_id not in app.state.in_flight_dispatches:
                    break
                await asyncio.sleep(0.02)
            for _ in range(2):
                try:
                    evt, _data = await asyncio.wait_for(queue.get(), timeout=1.0)
                    events.append(evt)
                except asyncio.TimeoutError:
                    break
        finally:
            inbox_events.bus.unsubscribe(sub_id)
        return events

    events = asyncio.run(_go())
    assert captured["source"] == SOURCE_SCHEDULED, "scheduled run dispatches on the scheduler path"
    assert captured["lifecycle_snapshot"] is None
    # A scheduled run passes the no-op chunk SINK (callable, NOT None) so dispatch_turn
    # HOLDS the turn open to its terminal result — same as a Chat turn — instead of an
    # async backend returning at prompt-submit and freeing the slot (Codex P2). The sink
    # discards chunks; the reply still surfaces over ``message.new``, not a live stream.
    assert captured["on_chunk"] is not None, "scheduled run holds the turn open via the no-op sink"
    assert callable(captured["on_chunk"]), "the held-open sink is the no-op chunk callable"
    assert captured["text"] == "digest please"
    assert captured["in_flight_while_running"] is True, "registered in_flight (Stop works) while running"
    assert events == ["turn.start", "turn.end"], "publishes the session turn lifecycle on the bus"
    assert session_id not in app.state.in_flight_dispatches, "slot released after the turn"


def test_hfr_482_create_per_run_delivery_adopts_reserved_session(monkeypatch, tmp_path):
    """A create-per-run Run adopts its freshly reserved Session at admission."""
    from core.scheduled_tasks import TaskExecutionStore
    from core.services import sessions as sessions_service
    from storage.background import attach_agent_run_delivery_in_connection

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session = _create_test_session(
        tmp_path,
        native_id="proj_create_per_run_delivery",
    )
    session_id = session["id"]
    request_store = TaskExecutionStore()
    run = request_store.enqueue_definition_run(
        definition_id="scheduled-create-per-run",
        run_type="scheduled",
        source_kind="scheduler",
        session_key="",
        session_id=None,
        post_to=None,
        deliver_key=None,
        prompt="fresh session prompt",
        agent_name="worker",
        session_policy="create_per_run",
    )
    assert request_store.claim(run.id) is not None

    started = asyncio.Event()

    async def _fake_dispatch_turn(
        ctrl, ctx, text, *, source=SOURCE_HUMAN, on_chunk=None, **_kwargs
    ):
        started.set()
        return TurnDispatchOutcome(error=None, settled_by=SETTLED_BY_TERMINAL_RESULT)

    monkeypatch.setattr(session_turns, "dispatch_turn_with_outcome", _fake_dispatch_turn)

    controller = _build_controller_double()
    internal_server.create_app(controller)
    ctx = MessageContext(
        user_id="workbench",
        channel_id=session_id,
        platform="avibe",
        message_id=f"scheduled:scheduled-create-per-run:{run.id}",
        platform_specific={
            "agent_session_id": session_id,
            "agent_session_target": {"agent_backend": "claude"},
            "task_execution_id": run.id,
            "task_trigger_kind": "scheduled",
            "task_definition_id": "scheduled-create-per-run",
        },
    )

    async def _go():
        result = await controller.session_turn_gate.submit_scheduled(
            session_id,
            ctx,
            "fresh session prompt",
            delivery_intent="queue",
        )
        await asyncio.wait_for(started.wait(), timeout=3)
        return result

    result = asyncio.run(_go())

    assert result.delivery_owner_transferred is True
    stored = request_store.get_run(run.id)
    assert stored is not None
    assert stored["session_id"] == session_id
    assert stored["delivery_id"]
    assert stored["metadata"]["delivery_outcome"] == {
        "intent": "queue",
        "status": "claimed",
        "target_was_busy": False,
    }
    with engine.connect() as conn:
        delivery = message_deliveries.get_delivery(conn, stored["delivery_id"])
    assert delivery is not None
    assert delivery["session_id"] == session_id

    unbound_existing = request_store.enqueue_definition_run(
        definition_id="scheduled-existing",
        run_type="scheduled",
        source_kind="scheduler",
        session_key="",
        session_id=None,
        post_to=None,
        deliver_key=None,
        prompt="must not adopt",
        agent_name="worker",
        session_policy="existing",
    )
    bound_create_per_run = request_store.enqueue_definition_run(
        definition_id="scheduled-bound-create-per-run",
        run_type="scheduled",
        source_kind="scheduler",
        session_key="",
        session_id=session_id,
        post_to=None,
        deliver_key=None,
        prompt="must not rebind",
        agent_name="worker",
        session_policy="create_per_run",
    )
    with engine.begin() as conn:
        other_session = sessions_service.create_session(
            conn,
            scope_id=session["scope_id"],
            agent_backend="claude",
            agent_name="worker",
        )
        other_delivery = _reserve_submission(
            conn,
            scope_id=session["scope_id"],
            session_id=other_session["id"],
            text="foreign delivery",
        )
        assert not attach_agent_run_delivery_in_connection(
            conn,
            unbound_existing.id,
            session_id=other_session["id"],
            delivery_id=other_delivery["id"],
        )
        assert not attach_agent_run_delivery_in_connection(
            conn,
            bound_create_per_run.id,
            session_id=other_session["id"],
            delivery_id=other_delivery["id"],
        )


def test_scheduled_gate_busy_enqueues_and_leaves_chat_turn_untouched(monkeypatch, tmp_path):
    """A scheduled run for a session that already has a turn in flight ENQUEUES a
    harness-attributed ``queued`` row (so it runs AFTER the active turn via the
    existing flush) instead of preempting it — and it never starts a competing
    turn nor disturbs the in-flight Chat task (Codex P2)."""
    from core.inbox_events import bus
    from storage import messages_service

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session, owner_turn_id = _create_active_test_turn(
        tmp_path,
        native_id="proj_sched_busy",
    )
    scope_id = session["scope_id"]
    session_id = session["id"]

    # A scheduled run must NEVER reach dispatch_turn while busy — a call here fails
    # the test loudly.
    async def _explode_dispatch_turn(*args, **kwargs):
        raise AssertionError("a busy scheduled run must enqueue, not dispatch a turn")

    monkeypatch.setattr(session_turns, "dispatch_turn_with_outcome", _explode_dispatch_turn)
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        bus,
        "publish",
        lambda event, payload: published.append((event, payload)),
    )

    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    ctx = MessageContext(
        user_id="workbench",
        channel_id=session_id,
        platform="avibe",
        message_id="watch:def-watch:scheduled-busy",
        platform_specific={
            "task_trigger_kind": "watch",
            "task_definition_id": "def-watch",
        },
    )

    async def _go():
        async def _busy():
            await asyncio.sleep(60)

        chat_task = asyncio.create_task(_busy())
        chat_ctx = MessageContext(user_id="U", channel_id="C", platform="avibe")
        app.state.in_flight_dispatches[session_id] = session_turns.Turn(
            task=chat_task,
            context=chat_ctx,
            logical_turn_id=owner_turn_id,
        )
        try:
            await controller.session_turn_gate.submit_scheduled(session_id, ctx, "scheduled while busy")
        finally:
            entry = app.state.in_flight_dispatches.get(session_id)
            # The in-flight Chat turn is undisturbed: same task object, not cancelled.
            assert entry is not None and entry.task is chat_task and not chat_task.done()
            chat_task.cancel()
        return chat_ctx

    chat_ctx = asyncio.run(_go())
    controller.message_handler.handle_user_message.assert_not_awaited()
    with engine.connect() as conn:
        queued = message_deliveries.list_queued(conn, session_id)
        # The queued row is drainable + carries the session's scope and harness
        # attribution; it stays OUT of the user transcript.
        transcript = messages_service.list_session_messages(conn, session_id=session_id, types=("user",))
    assert [q["text"] for q in queued] == ["scheduled while busy"]
    assert queued[0]["scope_id"] == scope_id
    assert queued[0]["author"] == "harness"
    assert queued[0]["author_name"] == "watch"
    assert queued[0]["author_id"] == "def-watch"
    assert [row["text"] for row in transcript["messages"]] == ["active owner"]
    assert ("queue.updated", {"session_id": session_id}) in published


def test_agent_run_send_now_steers_its_content_without_promoting_fifo(monkeypatch, tmp_path):
    """HFR-430: content-bearing send-now is P1 for that exact content."""
    from core.scheduled_tasks import TaskExecutionStore
    from core.services import sessions as sessions_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_agent_send_now",
            now="2026-07-30T00:00:00Z",
        )
        _seed_project_workdir(conn, scope_id, tmp_path)
        session_id = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="claude",
            agent_name="worker",
        )["id"]
        original = _reserve_submission(
            conn,
            scope_id=scope_id,
            session_id=session_id,
            text="original work",
        )

    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id=session_id,
        message="apply the correction",
        agent_name="worker",
        delivery_intent="send_now",
    )
    assert request_store.claim(request.id) is not None
    original_started = asyncio.Event()
    seen: list[tuple[str, str]] = []

    async def _dispatch(
        ctrl, ctx, text, *, source=SOURCE_HUMAN, on_chunk=None, **_kwargs
    ):
        seen.append((text, source))
        if text == "original work":
            _bind_test_native_start(engine, ctx)
            original_started.set()
            await asyncio.sleep(60)
        return TurnDispatchOutcome(error=None, settled_by=SETTLED_BY_TERMINAL_RESULT)

    monkeypatch.setattr(session_turns, "dispatch_turn_with_outcome", _dispatch)

    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)
    controller.session_turns._steer = AsyncMock(
        return_value=steer_result(SteerOutcome.ACCEPTED)
    )

    async def _go():
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                "/internal/dispatch_async",
                json={
                    "session_id": session_id,
                    "text": "original work",
                    "user_message_id": original["id"],
                },
            )
            assert response.status_code == 202
            await asyncio.wait_for(original_started.wait(), timeout=3)
            with engine.begin() as conn:
                message_deliveries.enqueue_queued(
                    conn,
                    scope_id=scope_id,
                    session_id=session_id,
                    text="older queued input",
                )
            context = MessageContext(
                user_id="workbench",
                channel_id=session_id,
                platform="avibe",
                message_id=f"agent_run:{request.id}",
                platform_specific={
                    "task_execution_id": request.id,
                    "task_trigger_kind": "agent_run",
                    "suppress_delivery": True,
                },
            )
            result = await controller.session_turn_gate.submit_scheduled(
                session_id,
                context,
                request.message or "",
                delivery_intent="send_now",
            )
            active = app.state.in_flight_dispatches[session_id]
            assert not active.task.done()
            active.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await active.task
            return result

    result = asyncio.run(_go())

    assert result == session_turns.TurnSubmissionResult(
        route="ran",
        queue_persisted=True,
        target_was_busy=True,
        delivery_status="accepted",
        delivery_owner_transferred=True,
    )
    controller.command_handler.handle_stop.assert_not_awaited()
    controller.session_turns._steer.assert_awaited_once()
    assert controller.session_turns._steer.await_args.args[1].text == "apply the correction"
    assert seen == [("original work", SOURCE_HUMAN)]
    with engine.connect() as conn:
        assert [row["text"] for row in message_deliveries.list_queued(conn, session_id)] == [
            "older queued input"
        ]
    stored = request_store.get_run(request.id)
    assert stored is not None
    assert stored["metadata"]["delivery_outcome"] == {
        "intent": "steer",
        "status": "accepted",
        "target_was_busy": True,
    }


def test_agent_run_send_now_steers_a_turn_started_during_admission(monkeypatch, tmp_path):
    """A stale idle snapshot cannot downgrade explicit P1 into P3 queueing."""
    from core.scheduled_tasks import TaskExecutionStore
    from core.services import sessions as sessions_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_agent_send_now_admission_race",
            now="2026-07-30T00:00:00Z",
        )
        _seed_project_workdir(conn, scope_id, tmp_path)
        session_id = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="claude",
            agent_name="worker",
        )["id"]

    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id=session_id,
        message="apply the late correction",
        agent_name="worker",
        delivery_intent="send_now",
    )
    assert request_store.claim(request.id) is not None
    controller = _build_controller_double()
    internal_server.create_app(controller)
    controller.session_turns._steer = AsyncMock(
        return_value=steer_result(SteerOutcome.ACCEPTED)
    )
    original_deliver = controller.session_turns.deliver
    inserted_racer = False

    async def _deliver_with_race(delivery_request, *, context=None):
        nonlocal inserted_racer
        if not inserted_racer:
            inserted_racer = True
            with engine.begin() as conn:
                owner = _reserve_submission(
                    conn,
                    scope_id=scope_id,
                    session_id=session_id,
                    text="racing owner",
                )
                turn_id = message_deliveries.new_turn_id()
                message_deliveries.insert_turn(
                    conn,
                    turn_id=turn_id,
                    session_id=session_id,
                    initial_delivery_id=owner["id"],
                    state="starting",
                    backend="claude",
                )
                claimed = message_deliveries.open_start_attempt(
                    conn,
                    owner["id"],
                    expected_version=1,
                    turn_id=turn_id,
                    attempt_id=message_deliveries.new_attempt_id(),
                )
                assert claimed is not None
                bound = message_deliveries.bind_native_start(
                    conn,
                    turn_id,
                    expected_version=int(
                        message_deliveries.get_turn(conn, turn_id)["version"]
                    ),
                    runtime_key=f"runtime:{session_id}",
                    runtime_turn_id=f"runtime-turn:{turn_id}",
                    native_turn_id=f"native:{turn_id}",
                )
                assert bound is not None
                accepted = message_deliveries.materialize_start_acceptance(
                    conn,
                    turn_id=turn_id,
                    evidence={"kind": "test_native_acceptance"},
                )
                assert accepted
        return await original_deliver(delivery_request, context=context)

    controller.session_turns.deliver = _deliver_with_race
    context = MessageContext(
        user_id="workbench",
        channel_id=session_id,
        platform="avibe",
        message_id=f"agent_run:{request.id}",
        platform_specific={
            "task_execution_id": request.id,
            "task_trigger_kind": "agent_run",
        },
    )

    async def _exercise():
        return await controller.session_turn_gate.submit_scheduled(
            session_id,
            context,
            request.message or "",
            delivery_intent="send_now",
        )

    result = asyncio.run(_exercise())

    assert result.target_was_busy is False
    assert result.delivery_status == "accepted"
    controller.session_turns._steer.assert_awaited_once()
    controller.command_handler.handle_stop.assert_not_awaited()
    with engine.connect() as conn:
        assert message_deliveries.list_queued(conn, session_id) == []


def test_agent_run_send_now_refusal_keeps_the_turn_and_queue(monkeypatch, tmp_path):
    """A refused interrupt is truthful and leaves both durable owners untouched."""
    from core.scheduled_tasks import TaskExecutionStore
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session, owner_turn_id = _create_active_test_turn(
        tmp_path,
        native_id="proj_agent_send_now_refused",
    )
    session_id = session["id"]

    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id=session_id,
        message="keep this queued",
        agent_name="worker",
        delivery_intent="send_now",
    )
    assert request_store.claim(request.id) is not None
    controller = _build_controller_double()

    async def _refuse_stop(context):
        context.platform_specific["stop_failure_reason"] = "refused"
        return False

    controller.command_handler.handle_stop = AsyncMock(side_effect=_refuse_stop)
    app = internal_server.create_app(controller)

    async def _go():
        task = asyncio.create_task(asyncio.sleep(60))
        app.state.in_flight_dispatches[session_id] = session_turns.Turn(
            task=task,
            context=MessageContext(user_id="U", channel_id=session_id, platform="avibe"),
            logical_turn_id=owner_turn_id,
        )
        context = MessageContext(
            user_id="workbench",
            channel_id=session_id,
            platform="avibe",
            message_id=f"agent_run:{request.id}",
            platform_specific={
                "task_execution_id": request.id,
                "task_trigger_kind": "agent_run",
            },
        )
        try:
            result = await controller.session_turn_gate.submit_scheduled(
                session_id,
                context,
                request.message or "",
                delivery_intent="send_now",
            )
            held = not task.done()
            return result, held
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    result, held = asyncio.run(_go())

    assert result == session_turns.TurnSubmissionResult(
        route="enqueued",
        queue_persisted=True,
        target_was_busy=True,
        delivery_status="queued",
        delivery_owner_transferred=True,
    )
    assert held is True
    with engine.connect() as conn:
        queued = message_deliveries.list_queued(conn, session_id)
        assert [row["text"] for row in queued] == [
            "keep this queued"
        ]
    stored = request_store.get_run(request.id)
    assert stored is not None
    assert stored["status"] == "running"
    assert stored["delivery_id"] == queued[0]["id"]
    assert "workbench_queue_holds_run" not in stored["metadata"]
    assert stored["metadata"]["delivery_outcome"] == {
        "intent": "steer",
        "status": "queued",
        "target_was_busy": True,
    }


def test_agent_run_send_now_retry_keeps_its_refused_p3_fallback_queued(monkeypatch, tmp_path):
    """A retry does not promote a P1 Delivery after it has fallen back to P3."""
    from core.scheduled_tasks import TaskExecutionStore

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session, _owner_turn_id = _create_active_test_turn(
        tmp_path,
        native_id="proj_agent_send_now_retry",
    )
    session_id = session["id"]
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id=session_id,
        message="retry this correction",
        agent_name="worker",
        delivery_intent="send_now",
    )
    assert request_store.claim(request.id) is not None
    controller = _build_controller_double()
    internal_server.create_app(controller)
    controller.session_turns._steer = AsyncMock(
        side_effect=(
            steer_result(SteerOutcome.REFUSED, reason="temporarily_unavailable"),
            steer_result(SteerOutcome.ACCEPTED),
        )
    )
    context = MessageContext(
        user_id="workbench",
        channel_id=session_id,
        platform="avibe",
        message_id=f"agent_run:{request.id}",
        platform_specific={
            "task_execution_id": request.id,
            "task_trigger_kind": "agent_run",
        },
    )

    async def _go():
        first = await controller.session_turn_gate.submit_scheduled(
            session_id,
            context,
            request.message or "",
            delivery_intent="send_now",
        )
        second = await controller.session_turn_gate.submit_scheduled(
            session_id,
            context,
            request.message or "",
            delivery_intent="send_now",
        )
        return first, second

    first, second = asyncio.run(_go())

    assert first.delivery_status == "queued"
    assert second.delivery_status == "queued"
    assert controller.session_turns._steer.await_count == 1
    with engine.connect() as conn:
        assert [row["text"] for row in message_deliveries.list_queued(conn, session_id)] == [
            "retry this correction"
        ]
    stored = request_store.get_run(request.id)
    assert stored is not None
    assert stored["metadata"]["delivery_outcome"] == {
        "intent": "steer",
        "status": "queued",
        "target_was_busy": True,
    }


def test_legacy_send_now_re_admits_unattempted_p3_delivery(monkeypatch, tmp_path):
    """An interrupted pre-cutover queue admission resumes as its own P1 content."""

    from core.scheduled_tasks import TaskExecutionStore

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session, _owner_turn_id = _create_active_test_turn(
        tmp_path,
        native_id="proj_legacy_send_now_re_admission",
    )
    session_id = session["id"]
    with engine.begin() as conn:
        older = message_deliveries.enqueue_queued(
            conn,
            scope_id=session["scope_id"],
            session_id=session_id,
            text="older queued work",
        )

    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id=session_id,
        message="legacy exact content",
        agent_name="worker",
        delivery_intent="queue",
    )
    assert request_store.claim(request.id) is not None
    controller = _build_controller_double()
    internal_server.create_app(controller)
    controller.session_turns._steer = AsyncMock(
        return_value=steer_result(SteerOutcome.ACCEPTED)
    )
    context = MessageContext(
        user_id="workbench",
        channel_id=session_id,
        platform="avibe",
        message_id=f"agent_run:{request.id}",
        platform_specific={
            "task_execution_id": request.id,
            "task_trigger_kind": "agent_run",
        },
    )

    async def _go():
        admitted = await controller.session_turn_gate.submit_scheduled(
            session_id,
            context,
            request.message or "",
            delivery_intent="queue",
        )
        resumed = await controller.session_turn_gate.submit_scheduled(
            session_id,
            context,
            request.message or "",
            delivery_intent="send_now",
        )
        return admitted, resumed

    admitted, resumed = asyncio.run(_go())

    assert admitted.delivery_status == "queued"
    assert resumed.delivery_status == "accepted"
    controller.session_turns._steer.assert_awaited_once()
    with engine.connect() as conn:
        queued = message_deliveries.list_queued(conn, session_id)
        stored = request_store.get_run(request.id)
        assert stored is not None
        delivery = message_deliveries.get_delivery(conn, stored["delivery_id"])
    assert [row["id"] for row in queued] == [older["id"]]
    assert delivery is not None
    assert message_deliveries.delivery_has_history_event(
        delivery,
        kind="legacy_send_now_re_admission",
    )
    assert message_deliveries.delivery_has_history_event(delivery, kind="steer")


def test_concurrent_agent_run_send_now_callers_steer_in_fifo_order(
    monkeypatch,
    tmp_path,
):
    """Concurrent send-now callers preserve FIFO and never invoke Stop."""
    from core.scheduled_tasks import TaskExecutionStore
    from core.services import sessions as sessions_service
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_agent_send_now_concurrent",
            now="2026-07-30T00:00:00Z",
        )
        _seed_project_workdir(conn, scope_id, tmp_path)
        session_id = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="claude",
            agent_name="worker",
        )["id"]
        original = _reserve_submission(
            conn,
            scope_id=scope_id,
            session_id=session_id,
            text="original work",
        )

    request_store = TaskExecutionStore()
    requests = [
        request_store.enqueue_agent_run(
            session_id=session_id,
            message=message,
            agent_name="worker",
            delivery_intent="send_now",
        )
        for message in ("first correction", "second correction")
    ]
    for request in requests:
        assert request_store.claim(request.id) is not None

    original_started = asyncio.Event()
    seen: list[str] = []

    async def _dispatch(
        ctrl, ctx, text, *, source=SOURCE_HUMAN, on_chunk=None, **_kwargs
    ):
        seen.append(text)
        _bind_test_native_start(engine, ctx)
        if text == "original work":
            original_started.set()
            await asyncio.sleep(60)
        return TurnDispatchOutcome(
            error=None,
            settled_by=SETTLED_BY_TERMINAL_RESULT,
        )

    monkeypatch.setattr(session_turns, "dispatch_turn_with_outcome", _dispatch)
    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    controller.session_turns._steer = AsyncMock(
        return_value=steer_result(SteerOutcome.ACCEPTED)
    )
    transport = httpx.ASGITransport(app=app)

    def _context(request):
        return MessageContext(
            user_id="workbench",
            channel_id=session_id,
            platform="avibe",
            message_id=f"agent_run:{request.id}",
            platform_specific={
                "task_execution_id": request.id,
                "task_trigger_kind": "agent_run",
            },
        )

    async def _go():
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/internal/dispatch_async",
                json={
                    "session_id": session_id,
                    "text": "original work",
                    "user_message_id": original["id"],
                },
            )
            assert response.status_code == 202
            await asyncio.wait_for(original_started.wait(), timeout=3)
            submissions = [
                asyncio.create_task(
                    controller.session_turn_gate.submit_scheduled(
                        session_id,
                        _context(request),
                        request.message or "",
                        delivery_intent="send_now",
                    )
                )
                for request in requests
            ]
            results = await asyncio.gather(*submissions)
            active = app.state.in_flight_dispatches[session_id]
            assert not active.task.done()
            active.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await active.task
            return results

    results = asyncio.run(_go())

    controller.command_handler.handle_stop.assert_not_awaited()
    assert controller.session_turns._steer.await_count == 2
    assert [result.delivery_status for result in results] == [
        "accepted",
        "accepted",
    ]
    assert all(result.route == "ran" for result in results)
    assert all(result.queue_persisted is True for result in results)
    assert all(result.target_was_busy is True for result in results)
    assert all(result.delivery_owner_transferred is True for result in results)
    assert seen == ["original work"]
    with engine.connect() as conn:
        assert message_deliveries.list_queued(conn, session_id) == []
    for request, expected_status in zip(
        requests,
        ("accepted", "accepted"),
        strict=True,
    ):
        stored = request_store.get_run(request.id)
        assert stored is not None
        assert stored["metadata"]["delivery_outcome"] == {
            "intent": "steer",
            "status": expected_status,
            "target_was_busy": True,
        }


def test_agent_run_send_now_restart_preserves_refused_queue_behind_durable_owner(
    monkeypatch,
    tmp_path,
):
    """Restart neither replays Stop nor bypasses the still-active durable owner."""
    from core.scheduled_tasks import TaskExecutionStore

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session, owner_turn_id = _create_active_test_turn(
        tmp_path,
        native_id="proj_agent_send_now_restart",
    )
    session_id = session["id"]

    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id=session_id,
        message="recover after restart",
        agent_name="worker",
        delivery_intent="send_now",
    )
    assert request_store.claim(request.id) is not None
    first_controller = _build_controller_double()

    async def _refuse_stop(stop_context):
        stop_context.platform_specific["stop_failure_reason"] = "refused"
        return False

    first_controller.command_handler.handle_stop = AsyncMock(side_effect=_refuse_stop)
    first_app = internal_server.create_app(first_controller)
    context = MessageContext(
        user_id="workbench",
        channel_id=session_id,
        platform="avibe",
        message_id=f"agent_run:{request.id}",
        platform_specific={
            "task_execution_id": request.id,
            "task_trigger_kind": "agent_run",
        },
    )

    async def _admit_then_restart():
        task = asyncio.create_task(asyncio.sleep(60))
        first_app.state.in_flight_dispatches[session_id] = session_turns.Turn(
            task=task,
            context=MessageContext(
                user_id="U",
                channel_id=session_id,
                platform="avibe",
            ),
            logical_turn_id=owner_turn_id,
        )
        try:
            result = await first_controller.session_turn_gate.submit_scheduled(
                session_id,
                context,
                request.message or "",
                delivery_intent="send_now",
            )
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return result

    admitted = asyncio.run(_admit_then_restart())
    assert admitted.delivery_status == "queued"
    with engine.connect() as conn:
        assert [row["text"] for row in message_deliveries.list_queued(conn, session_id)] == [
            "recover after restart"
        ]

    seen: list[str] = []

    async def _dispatch(
        ctrl, ctx, text, *, source=SOURCE_HUMAN, on_chunk=None, **_kwargs
    ):
        seen.append(text)
        return TurnDispatchOutcome(
            error=None,
            settled_by=SETTLED_BY_TERMINAL_RESULT,
        )

    monkeypatch.setattr(session_turns, "dispatch_turn_with_outcome", _dispatch)
    second_controller = _build_controller_double()
    internal_server.create_app(second_controller)

    async def _recover():
        recovered = await second_controller.session_turns.recover_persisted_agent_run_queue(
            session_id
        )
        for _ in range(200):
            if session_id not in second_controller.session_turns.in_flight:
                break
            await asyncio.sleep(0.01)
        return recovered

    recovered = asyncio.run(_recover())

    assert recovered == []
    assert seen == []
    second_controller.command_handler.handle_stop.assert_not_awaited()
    with engine.connect() as conn:
        assert [row["text"] for row in message_deliveries.list_queued(conn, session_id)] == [
            "recover after restart"
        ]
    stored = request_store.get_run(request.id)
    assert stored is not None
    assert stored["metadata"]["delivery_intent"] == "steer"
    assert stored["metadata"]["delivery_outcome"]["status"] == "queued"


def test_agent_run_send_now_on_an_idle_backlog_starts_its_own_content(monkeypatch, tmp_path):
    """Content-bearing P1 starts itself and leaves an older P3 head queued."""
    from core.scheduled_tasks import TaskExecutionStore
    from core.services import sessions as sessions_service
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_agent_send_now_idle",
            now="2026-07-30T00:00:00Z",
        )
        _seed_project_workdir(conn, scope_id, tmp_path)
        session_id = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="claude",
            agent_name="worker",
        )["id"]
        message_deliveries.enqueue_queued(
            conn,
            scope_id=scope_id,
            session_id=session_id,
            text="older queued input",
        )

    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id=session_id,
        message="new urgent input",
        agent_name="worker",
        delivery_intent="send_now",
    )
    assert request_store.claim(request.id) is not None
    seen: list[str] = []

    async def _dispatch(
        ctrl, ctx, text, *, source=SOURCE_HUMAN, on_chunk=None, **_kwargs
    ):
        seen.append(text)
        return TurnDispatchOutcome(error=None, settled_by=SETTLED_BY_TERMINAL_RESULT)

    monkeypatch.setattr(session_turns, "dispatch_turn_with_outcome", _dispatch)
    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    context = MessageContext(
        user_id="workbench",
        channel_id=session_id,
        platform="avibe",
        message_id=f"agent_run:{request.id}",
        platform_specific={
            "task_execution_id": request.id,
            "task_trigger_kind": "agent_run",
        },
    )

    async def _go():
        result = await controller.session_turn_gate.submit_scheduled(
            session_id,
            context,
            request.message or "",
            delivery_intent="send_now",
        )
        for _ in range(300):
            if len(seen) == 1:
                break
            await asyncio.sleep(0.01)
        return result

    result = asyncio.run(_go())

    assert result == session_turns.TurnSubmissionResult(
        route="ran",
        queue_persisted=True,
        target_was_busy=False,
        delivery_status="claimed",
        delivery_owner_transferred=True,
    )
    assert seen == ["new urgent input"]
    with engine.connect() as conn:
        assert [row["text"] for row in message_deliveries.list_queued(conn, session_id)] == [
            "older queued input"
        ]
    controller.command_handler.handle_stop.assert_not_awaited()


def test_agent_run_send_now_idle_start_failure_is_reconciled_without_replay(
    monkeypatch,
    tmp_path,
):
    from core.scheduled_tasks import TaskExecutionStore
    from core.services import sessions as sessions_service
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_send_now_recovery",
            now="2026-07-30T00:00:00Z",
        )
        _seed_project_workdir(conn, scope_id, tmp_path)
        session_id = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="claude",
            agent_name="worker",
        )["id"]

    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id=session_id,
        message="retry the idle flush",
        agent_name="worker",
        delivery_intent="send_now",
    )
    assert request_store.claim(request.id) is not None
    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    controller.emit_agent_message = AsyncMock()

    async def _ambiguous_dispatch(*args, **kwargs):
        raise RuntimeError("native start outcome lost")

    monkeypatch.setattr(session_turns, "dispatch_turn_with_outcome", _ambiguous_dispatch)
    context = MessageContext(
        user_id="workbench",
        channel_id=session_id,
        platform="avibe",
        message_id=f"agent_run:{request.id}",
        platform_specific={
            "task_execution_id": request.id,
            "task_trigger_kind": "agent_run",
        },
    )

    async def _go():
        result = await controller.session_turn_gate.submit_scheduled(
            session_id,
            context,
            request.message or "",
            delivery_intent="send_now",
        )
        for _ in range(300):
            if session_id not in app.state.in_flight_dispatches:
                break
            await asyncio.sleep(0.01)
        return result

    result = asyncio.run(_go())

    assert result == session_turns.TurnSubmissionResult(
        route="ran",
        queue_persisted=True,
        target_was_busy=False,
        delivery_status="claimed",
        delivery_owner_transferred=True,
    )
    with engine.connect() as conn:
        delivery = message_deliveries.get_delivery_by_dedupe(
            conn,
            message_deliveries.native_dedupe_key(
                "avibe",
                f"agent_run:{request.id}",
                scope_id=scope_id,
            ),
        )
        assert delivery is not None
        assert delivery["state"] == "claimed"
        turn = message_deliveries.get_turn(conn, delivery["turn_id"])
        assert turn is not None
        assert turn["start_receipt_outcome"] == "unknown"
        assert message_deliveries.list_queued(conn, session_id) == []
    stored = request_store.get_run(request.id)
    assert stored is not None
    assert stored["status"] == "running"
    assert stored["delivery_id"] == delivery["id"]
    assert "workbench_queue_holds_run" not in stored["metadata"]


def test_canceling_held_agent_run_retires_queue_before_send_now(
    monkeypatch,
    tmp_path,
):
    """A canceled queue owner cannot leave stale work that triggers Stop."""

    from core.scheduled_tasks import TaskExecutionStore
    from core.services import sessions as sessions_service
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_cancel_before_send_now",
            now="2026-07-30T00:00:00Z",
        )
        _seed_project_workdir(conn, scope_id, tmp_path)
        session_id = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="claude",
            agent_name="worker",
        )["id"]

    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id=session_id,
        message="obsolete urgent correction",
        agent_name="worker",
        delivery_intent="send_now",
    )
    assert request_store.claim(request.id) is not None
    request_store.requeue(request.id)
    with engine.begin() as conn:
        queued_delivery = message_deliveries.enqueue_queued(
            conn,
            scope_id=scope_id,
            session_id=session_id,
            platform="avibe",
            author="harness",
            source="harness",
            message_type="harness",
            native_message_id=f"agent_run:{request.id}",
            text=request.message or "",
        )
        from storage.background import attach_agent_run_delivery_in_connection

        assert attach_agent_run_delivery_in_connection(
            conn,
            request.id,
            session_id=session_id,
            delivery_id=queued_delivery["id"],
        )

    controller = _build_controller_double()
    app = internal_server.create_app(controller)

    async def _exercise():
        active_task = asyncio.create_task(asyncio.sleep(60))
        app.state.in_flight_dispatches[session_id] = session_turns.Turn(
            task=active_task,
            context=MessageContext(
                user_id="workbench",
                channel_id=session_id,
                platform="avibe",
            ),
        )
        try:
            assert request_store.cancel_run(request.id) is True
            return await controller.session_turns.send_now(session_id)
        finally:
            active_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await active_task

    result = asyncio.run(_exercise())

    assert result == {
        "ok": True,
        "session_id": session_id,
        "status": "empty",
    }
    controller.command_handler.handle_stop.assert_not_awaited()
    with engine.connect() as conn:
        assert message_deliveries.list_queued(conn, session_id) == []
    stored = request_store.get_run(request.id)
    assert stored is not None
    assert stored["status"] == "canceled"
    assert stored["cancel_requested"] is True


def test_idle_send_now_releases_hold_and_starts_the_exact_head(
    monkeypatch,
    tmp_path,
):
    """Empty P1 releases a hold and claims only the observed FIFO head."""

    from core.services import sessions as sessions_service
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_idle_flush_failed",
            now="2026-07-30T00:00:00Z",
        )
        _seed_project_workdir(conn, scope_id, tmp_path)
        session_id = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="claude",
            agent_name="worker",
        )["id"]
        queued = message_deliveries.enqueue_queued(
            conn,
            scope_id=scope_id,
            session_id=session_id,
            text="retry me later",
        )

    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    controller.emit_agent_message = AsyncMock()

    async def _dispatch(
        ctrl, context, text, *, source=SOURCE_HUMAN, on_chunk=None, **_kwargs
    ):
        assert text == "retry me later"
        _bind_test_native_start(engine, context)
        return TurnDispatchOutcome(error=None, settled_by=SETTLED_BY_TERMINAL_RESULT)

    monkeypatch.setattr(session_turns, "dispatch_turn_with_outcome", _dispatch)

    async def _exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.post(f"/internal/send-now/{session_id}")
            for _ in range(300):
                if session_id not in app.state.in_flight_dispatches:
                    break
                await asyncio.sleep(0.01)
            return response

    response = asyncio.run(_exercise())

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "session_id": session_id,
        "status": "claimed",
        "delivery_id": queued["id"],
    }
    with engine.connect() as conn:
        assert message_deliveries.list_queued(conn, session_id) == []
        assert messages_service.get_message(conn, queued["id"], session_id=session_id)


def test_agent_run_send_now_cancel_race_never_becomes_failed(monkeypatch, tmp_path):
    from core.scheduled_tasks import (
        ScheduledTaskService,
        ScheduledTaskStore,
        TaskExecutionStore,
    )
    from core.services import sessions as sessions_service
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_send_now_cancel",
            now="2026-07-30T00:00:00Z",
        )
        _seed_project_workdir(conn, scope_id, tmp_path)
        session_id = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="claude",
            agent_name="worker",
        )["id"]

    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id=session_id,
        message="must not survive cancellation",
        agent_name="worker",
        delivery_intent="send_now",
    )
    controller = _build_controller_double()
    controller.agent_service.runtime_activation_identity_for_request = Mock(
        return_value=None
    )
    controller.platform_settings_managers = {}
    controller.im_clients = {"avibe": SimpleNamespace()}
    controller.get_im_client_for_context = lambda _context: SimpleNamespace(
        should_use_thread_for_reply=lambda: True,
        should_use_thread_for_dm_session=lambda: False,
    )
    internal_server.create_app(controller)
    submit_scheduled = controller.session_turn_gate.submit_scheduled

    async def _cancel_after_claim(*args, **kwargs):
        sqlite_store = request_store._sqlite
        assert sqlite_store is not None
        assert sqlite_store.cancel_run(request.id) is True
        return await submit_scheduled(*args, **kwargs)

    controller.session_turn_gate.submit_scheduled = _cancel_after_claim
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    async def _exercise() -> None:
        await service._drain_requests()
        execution = service._inflight_executions.get(request.id)
        assert execution is not None
        await execution

    asyncio.run(_exercise())

    stored = request_store.get_run(request.id)
    assert stored is not None
    assert stored["status"] == "canceled"
    assert stored["cancel_requested"] is True
    assert stored["error"] is None
    assert stored["metadata"]["delivery_outcome"]["status"] == "canceled"
    with engine.connect() as conn:
        assert message_deliveries.list_queued(conn, session_id) == []


def test_scheduled_gate_busy_duplicate_native_id_is_skipped(monkeypatch, tmp_path):
    """A retried harness callback already sitting in the queue is a duplicate,
    not a scheduler failure."""
    from core.services import sessions as sessions_service
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn, platform="avibe", scope_type="project", native_id="proj_sched_dup", now="2026-05-31T00:00:00Z"
        )
        _seed_project_workdir(conn, scope_id, tmp_path)
        session = sessions_service.create_session(
            conn, scope_id=scope_id, agent_backend="claude", agent_name="worker"
        )
        active_delivery = _reserve_submission(
            conn,
            scope_id=scope_id,
            session_id=session["id"],
            text="running",
            author="harness",
            source="harness",
            message_type="harness",
            native_message_id="watch:def-watch:running",
        )
        active_turn_id = message_deliveries.new_turn_id()
        message_deliveries.insert_turn(
            conn,
            turn_id=active_turn_id,
            session_id=session["id"],
            initial_delivery_id=active_delivery["id"],
            state="starting",
            backend="claude",
        )
        opened = message_deliveries.open_start_attempt(
            conn,
            active_delivery["id"],
            expected_version=1,
            turn_id=active_turn_id,
            attempt_id=message_deliveries.new_attempt_id(),
        )
        assert opened is not None
        bound = message_deliveries.bind_native_start(
            conn,
            active_turn_id,
            expected_version=int(
                message_deliveries.get_turn(conn, active_turn_id)["version"]
            ),
            runtime_key=f"runtime:{session['id']}",
            runtime_turn_id=f"runtime-turn:{active_turn_id}",
            native_turn_id=f"native:{active_turn_id}",
        )
        assert bound is not None
        accepted = message_deliveries.materialize_start_acceptance(
            conn,
            turn_id=active_turn_id,
            evidence={"kind": "test_native_acceptance"},
        )
        assert accepted
        messages_service.append(
            conn,
            scope_id=scope_id,
            session_id=session["id"],
            platform="avibe",
            author="harness",
            source="harness",
            message_type="harness",
            text="accepted before delivery migration",
            native_message_id="watch:def-watch:legacy-accepted",
        )
    session_id = session["id"]

    async def _explode_dispatch_turn(*args, **kwargs):
        raise AssertionError("a busy scheduled run must enqueue, not dispatch a turn")

    monkeypatch.setattr(session_turns, "dispatch_turn_with_outcome", _explode_dispatch_turn)

    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    ctx = MessageContext(
        user_id="workbench",
        channel_id=session_id,
        platform="avibe",
        message_id="watch:def-watch:run-1",
        platform_specific={
            "task_trigger_kind": "watch",
            "task_definition_id": "def-watch",
        },
    )

    async def _go():
        async def _busy():
            await asyncio.sleep(60)

        chat_task = asyncio.create_task(_busy())
        app.state.in_flight_dispatches[session_id] = session_turns.Turn(
            task=chat_task,
            context=MessageContext(
                user_id="U",
                channel_id="C",
                platform="avibe",
                message_id="watch:def-watch:running",
            ),
            logical_turn_id=active_turn_id,
        )
        try:
            first = await controller.session_turn_gate.submit_scheduled(session_id, ctx, "first")
            second = await controller.session_turn_gate.submit_scheduled(session_id, ctx, "duplicate")
            running_ctx = MessageContext(
                user_id="workbench",
                channel_id=session_id,
                platform="avibe",
                message_id="watch:def-watch:running",
            )
            running_duplicate = await controller.session_turn_gate.submit_scheduled(
                session_id,
                running_ctx,
                "running duplicate",
            )
            legacy_ctx = MessageContext(
                user_id="workbench",
                channel_id=session_id,
                platform="avibe",
                message_id="watch:def-watch:legacy-accepted",
            )
            legacy_duplicate = await controller.session_turn_gate.submit_scheduled(
                session_id,
                legacy_ctx,
                "legacy accepted duplicate",
            )
        finally:
            chat_task.cancel()
        return first, second, running_duplicate, legacy_duplicate

    assert asyncio.run(_go()) == ("enqueued", "duplicate", "duplicate", "duplicate")
    with engine.connect() as conn:
        queued = message_deliveries.list_queued(conn, session_id)
    assert [row["text"] for row in queued] == ["first"]


def test_scheduled_gate_retry_resumes_matching_reserved_delivery(monkeypatch, tmp_path):
    """A retry resumes a pre-claim reservation instead of completing as duplicate."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session = _create_test_session(
        tmp_path,
        native_id="proj_sched_reserved_retry",
    )
    session_id = session["id"]
    dispatched: list[str] = []

    async def _accept_dispatch(
        ctrl,
        ctx,
        text,
        *,
        source=SOURCE_HUMAN,
        on_chunk=None,
        lifecycle_snapshot=None,
    ):
        del lifecycle_snapshot
        dispatched.append(text)
        _bind_test_native_start(engine, ctx)
        return TurnDispatchOutcome(error=None, settled_by=SETTLED_BY_TERMINAL_RESULT)

    monkeypatch.setattr(session_turns, "dispatch_turn_with_outcome", _accept_dispatch)

    controller = _build_controller_double()
    internal_server.create_app(controller)
    manager = controller.session_turns
    submit_scheduled = controller.session_turn_gate.submit_scheduled
    resolve_backend = manager._delivery_backend
    fail_before_claim = True

    def _resolve_backend(session_id, context):
        nonlocal fail_before_claim
        if fail_before_claim:
            fail_before_claim = False
            raise RuntimeError("backend temporarily unresolved")
        return resolve_backend(session_id, context)

    monkeypatch.setattr(manager, "_delivery_backend", _resolve_backend)
    ctx = MessageContext(
        user_id="workbench",
        channel_id=session_id,
        platform="avibe",
        message_id="watch:def-watch:reserved-retry",
        platform_specific={
            "agent_session_target": {"agent_backend": "claude"},
            "task_trigger_kind": "watch",
            "task_definition_id": "def-watch",
        },
    )

    async def _go():
        with pytest.raises(RuntimeError, match="temporarily unresolved"):
            await submit_scheduled(session_id, ctx, "original prompt")
        with engine.connect() as conn:
            reserved = message_deliveries.get_delivery_by_dedupe(
                conn,
                message_deliveries.native_dedupe_key(
                    "avibe",
                    "watch:def-watch:reserved-retry",
                    scope_id=session["scope_id"],
                ),
            )
            assert reserved is not None
            assert reserved["state"] == "reserved"

        resumed = await submit_scheduled(
            session_id,
            ctx,
            "changed retry payload",
        )
        duplicate = await submit_scheduled(session_id, ctx, "duplicate")
        return resumed, duplicate, reserved["id"]

    resumed, duplicate, delivery_id = asyncio.run(_go())
    assert (resumed, duplicate) == ("ran", "duplicate")
    assert dispatched == ["original prompt"]
    with engine.connect() as conn:
        delivery = message_deliveries.get_delivery(conn, delivery_id)
        assert delivery is not None
        assert delivery["state"] == "accepted"
        assert delivery["message_id"] == delivery_id


def test_scheduled_gate_retry_preserves_existing_delivery_owner(monkeypatch, tmp_path):
    from core.scheduled_tasks import TaskExecutionStore
    from storage.background import (
        attach_agent_run_delivery_in_connection,
        run_update_event_transaction,
    )

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session = _create_test_session(
        tmp_path,
        native_id="proj_sched_owned_retry",
    )
    session_id = session["id"]
    request_store = TaskExecutionStore()
    run = request_store.enqueue_hook_send(
        session_key="",
        session_id=session_id,
        prompt="owned watch result",
        run_type="watch",
    )
    assert request_store.claim(run.id) is not None
    delivery_id = message_deliveries.new_delivery_id()
    with run_update_event_transaction(engine) as conn:
        message_deliveries.insert_delivery(
            conn,
            delivery_id=delivery_id,
            session_id=session_id,
            priority="p3",
            state="queued",
            snapshot=message_deliveries.message_snapshot(
                scope_id=session["scope_id"],
                session_id=session_id,
                platform="avibe",
                author="harness",
                source="harness",
                message_type="harness",
                text="owned watch result",
                native_message_id=f"watch:{run.id}",
            ),
            dispatch_text="owned watch result",
            dedupe_key=f"avibe:watch:{run.id}",
        )
        assert attach_agent_run_delivery_in_connection(
            conn,
            run.id,
            session_id=session_id,
            delivery_id=delivery_id,
        )

    controller = _build_controller_double()
    internal_server.create_app(controller)
    context = MessageContext(
        user_id="workbench",
        channel_id=session_id,
        platform="avibe",
        message_id=f"watch:{run.id}",
        platform_specific={
            "task_execution_id": run.id,
            "task_trigger_kind": "watch",
        },
    )

    result = asyncio.run(
        controller.session_turn_gate.submit_scheduled(
            session_id,
            context,
            "owned watch result",
        )
    )

    assert result == session_turns.TurnSubmissionResult(
        route="enqueued",
        queue_persisted=True,
        target_was_busy=False,
        delivery_status="queued",
        delivery_owner_transferred=True,
    )
    assert request_store.get_run(run.id)["status"] == "running"


def test_scheduled_gate_cancellation_preserves_transferred_delivery_owner(
    monkeypatch,
    tmp_path,
):
    """A canceled executor cannot take a natively accepted steer back from Delivery."""
    from core.scheduled_tasks import TaskExecutionStore

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session, _active_turn_id = _create_active_test_turn(
        tmp_path,
        native_id="proj_sched_canceled_handoff",
    )
    session_id = session["id"]
    request_store = TaskExecutionStore()
    run = request_store.enqueue_agent_run(
        session_id=session_id,
        message="steer while active",
        agent_name="worker",
    )
    assert request_store.claim(run.id) is not None

    controller = _build_controller_double()
    internal_server.create_app(controller)
    manager = controller.session_turns
    native_write_started = asyncio.Event()
    release_native_write = asyncio.Event()

    async def _natively_accepted_delivery(request, *, context):
        del request, context
        native_write = asyncio.create_task(release_native_write.wait())
        native_write_started.set()
        try:
            await asyncio.shield(native_write)
        except asyncio.CancelledError:
            # Match the shared steering boundary: native receipt is reconciled
            # before cancellation propagates to durable admission.
            await native_write
            raise

    monkeypatch.setattr(manager, "deliver", _natively_accepted_delivery)
    context = MessageContext(
        user_id="workbench",
        channel_id=session_id,
        platform="avibe",
        message_id=f"agent_run:{run.id}",
        platform_specific={
            "task_execution_id": run.id,
            "task_trigger_kind": "agent_run",
        },
    )

    async def _go():
        submission = asyncio.create_task(
            controller.session_turn_gate.submit_scheduled(
                session_id,
                context,
                "steer while active",
            )
        )
        await native_write_started.wait()
        submission.cancel()
        await asyncio.sleep(0)
        assert not submission.done()
        release_native_write.set()
        return await submission

    result = asyncio.run(_go())

    assert result == session_turns.TurnSubmissionResult(
        route="enqueued",
        queue_persisted=True,
        target_was_busy=True,
        delivery_status="reserved",
        delivery_owner_transferred=True,
    )
    stored = request_store.get_run(run.id)
    assert stored is not None
    assert stored["status"] == "running"
    assert stored["delivery_id"]
    with engine.connect() as conn:
        delivery = message_deliveries.get_delivery(conn, stored["delivery_id"])
    assert delivery is not None
    assert delivery["state"] == "reserved"


def test_scheduled_send_now_recovers_transferred_reservation(monkeypatch, tmp_path):
    """After ownership transfer, a pre-claim failure belongs to Delivery recovery."""
    from core.scheduled_tasks import TaskExecutionStore

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session = _create_test_session(
        tmp_path,
        native_id="proj_sched_send_now_reserved_retry",
    )
    session_id = session["id"]
    request_store = TaskExecutionStore()
    run = request_store.enqueue_agent_run(
        session_id=session_id,
        message="original send-now prompt",
        agent_name="worker",
        delivery_intent="send_now",
    )
    assert request_store.claim(run.id) is not None
    dispatched: list[str] = []

    async def _accept_dispatch(
        ctrl,
        ctx,
        text,
        *,
        source=SOURCE_HUMAN,
        on_chunk=None,
        lifecycle_snapshot=None,
    ):
        del lifecycle_snapshot
        dispatched.append(text)
        _bind_test_native_start(engine, ctx)
        return TurnDispatchOutcome(error=None, settled_by=SETTLED_BY_TERMINAL_RESULT)

    monkeypatch.setattr(session_turns, "dispatch_turn_with_outcome", _accept_dispatch)

    controller = _build_controller_double()
    internal_server.create_app(controller)
    manager = controller.session_turns
    submit_scheduled = controller.session_turn_gate.submit_scheduled
    resolve_backend = manager._delivery_backend
    fail_before_claim = True

    def _resolve_backend(session_id, context):
        nonlocal fail_before_claim
        if fail_before_claim:
            fail_before_claim = False
            raise RuntimeError("backend temporarily unresolved")
        return resolve_backend(session_id, context)

    monkeypatch.setattr(manager, "_delivery_backend", _resolve_backend)
    ctx = MessageContext(
        user_id="workbench",
        channel_id=session_id,
        platform="avibe",
        message_id=f"agent_run:{run.id}",
        platform_specific={
            "agent_session_target": {"agent_backend": "claude"},
            "task_execution_id": run.id,
            "task_trigger_kind": "agent_run",
        },
    )

    async def _go():
        deferred = await submit_scheduled(
            session_id,
            ctx,
            "original send-now prompt",
            delivery_intent="send_now",
        )
        assert isinstance(deferred, session_turns.TurnSubmissionResult)
        assert deferred.route == "enqueued"
        assert deferred.delivery_status == "reserved"
        assert deferred.delivery_owner_transferred is True
        held = request_store.get_run(run.id)
        assert held is not None
        assert held["status"] == "running"
        assert held["delivery_id"] is not None
        assert "workbench_queue_holds_run" not in held["metadata"]

        await manager.recover_durable_delivery_state(session_id)
        resumed_owner = await submit_scheduled(
            session_id,
            ctx,
            "duplicate",
            delivery_intent="send_now",
        )
        return resumed_owner

    resumed_owner = asyncio.run(_go())
    assert isinstance(resumed_owner, session_turns.TurnSubmissionResult)
    assert resumed_owner.route == "ran"
    assert resumed_owner.delivery_status == "claimed"
    assert resumed_owner.delivery_owner_transferred is True
    assert dispatched == ["original send-now prompt"]


def test_scheduled_gate_cancel_stops_scheduled_run(monkeypatch, tmp_path):
    """Stop works for a scheduled run: because the run goes through ``_run_turn``
    it registers the scheduled ``context`` in ``in_flight``, so
    ``/internal/cancel/{session_id}`` finds the task + reuses the IM ``/stop`` path
    to interrupt the backend (mirrors the Chat cancel test)."""
    from core.services import sessions as sessions_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn, platform="avibe", scope_type="project", native_id="proj_sched_cancel", now="2026-05-31T00:00:00Z"
        )
        _seed_project_workdir(conn, scope_id, tmp_path)
        session = sessions_service.create_session(
            conn, scope_id=scope_id, agent_backend="claude", agent_name="worker"
        )
    session_id = session["id"]

    started = asyncio.Event()

    async def _long_dispatch_turn(
        ctrl, ctx, text, *, source=SOURCE_HUMAN, on_chunk=None, **_kwargs
    ):
        _bind_test_native_start(engine, ctx)
        started.set()
        await asyncio.sleep(5)  # held until the test cancels it
        return TurnDispatchOutcome(error=None, settled_by=SETTLED_BY_TERMINAL_RESULT)

    monkeypatch.setattr(session_turns, "dispatch_turn_with_outcome", _long_dispatch_turn)

    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)
    ctx = MessageContext(user_id="workbench", channel_id=session_id, platform="avibe")

    async def _go():
        # Start the scheduled run in the background (it holds in_flight open).
        run = asyncio.create_task(controller.session_turn_gate.submit_scheduled(session_id, ctx, "scheduled run"))
        await asyncio.wait_for(started.wait(), timeout=3)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(f"/internal/cancel/{session_id}")
        for _ in range(200):
            if session_id not in app.state.in_flight_dispatches:
                break
            await asyncio.sleep(0.02)
        run.cancel()
        return resp

    resp = asyncio.run(_go())
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancel_requested"
    # The cancel interrupted the backend through the IM /stop path with the
    # scheduled run's own context.
    controller.command_handler.handle_stop.assert_awaited_once()
    assert session_id not in app.state.in_flight_dispatches, "slot released after the scheduled run was stopped"


def test_hfr_476_run_cancel_does_not_stop_a_shared_turn(monkeypatch, tmp_path):
    """A Run accepted as one Turn participant cannot issue Session-wide Stop."""

    from core.scheduled_tasks import TaskExecutionStore
    from storage.background import attach_agent_run_delivery_in_connection
    from storage.models import message_deliveries as delivery_rows

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session, turn_id = _create_active_test_turn(
        tmp_path,
        native_id="proj_shared_run_cancel",
    )
    session_id = session["id"]
    request_store = TaskExecutionStore()
    run = request_store.enqueue_agent_run(
        session_id=session_id,
        message="steered participant",
        agent_name="worker",
        callback_session_id="ses_callback",
    )
    assert request_store.claim(run.id) is not None

    with engine.begin() as conn:
        initial = message_deliveries.delivery_for_turn(conn, turn_id)
        assert initial is not None
        steer_id = message_deliveries.new_delivery_id()
        values = dict(initial)
        values.update(
            id=steer_id,
            priority="p1",
            dedupe_key=None,
            turn_role="steer",
            turn_position=1,
            submitted_at="2026-08-11T11:00:00Z",
            updated_at="2026-08-11T11:00:00Z",
            version=1,
        )
        conn.execute(delivery_rows.insert().values(**values))
        assert attach_agent_run_delivery_in_connection(
            conn,
            run.id,
            session_id=session_id,
            delivery_id=steer_id,
        )

    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post(
                f"/internal/cancel/{session_id}",
                params={"run_id": run.id},
            )

    response = asyncio.run(_go())

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "session_id": session_id,
        "status": "run_detached",
        "reason": "run_is_steered_participant",
    }
    controller.command_handler.handle_stop.assert_not_awaited()
    with engine.connect() as conn:
        turn = message_deliveries.get_turn(conn, turn_id)
    assert turn is not None
    assert turn["state"] == "active"
    assert turn["control_state"] is None
    saved = request_store.get_run(run.id)
    assert saved is not None
    assert saved["status"] == "canceled"
    assert saved["callback_status"] == "skipped"
    assert saved["callback_completed_at"] is not None
    assert request_store.list_pending_callbacks() == []


def test_run_cancel_guard_counts_an_unresolved_steer_as_a_participant(
    monkeypatch,
    tmp_path,
):
    """A native steer in flight prevents the initial Run from stopping the Turn."""

    from core.scheduled_tasks import TaskExecutionStore
    from storage.background import attach_agent_run_delivery_in_connection

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session, turn_id = _create_active_test_turn(
        tmp_path,
        native_id="proj_unresolved_steer_cancel",
    )
    session_id = session["id"]
    request_store = TaskExecutionStore()
    owner_run = request_store.enqueue_agent_run(
        session_id=session_id,
        message="initial owner",
        agent_name="worker",
        callback_session_id="ses_callback",
    )
    assert request_store.claim(owner_run.id) is not None

    with engine.begin() as conn:
        turn = message_deliveries.get_turn(conn, turn_id)
        initial = message_deliveries.delivery_for_turn(conn, turn_id)
        assert turn is not None
        assert initial is not None
        assert attach_agent_run_delivery_in_connection(
            conn,
            owner_run.id,
            session_id=session_id,
            delivery_id=initial["id"],
        )
        steer = _reserve_submission(
            conn,
            scope_id=session["scope_id"],
            session_id=session_id,
            text="second input already steering",
        )
        steer_id = str(steer["id"])
        claimed = message_deliveries.open_steer_attempt(
            conn,
            steer["id"],
            expected_version=int(steer["version"]),
            turn_id=turn_id,
            attempt_id=message_deliveries.new_attempt_id(),
            expected_native_turn_id=str(turn["native_turn_id"]),
        )
        assert claimed is not None
        assert claimed["state"] == "steering"

    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post(
                f"/internal/cancel/{session_id}",
                params={"run_id": owner_run.id},
            )

    response = asyncio.run(_go())

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "session_id": session_id,
        "status": "run_detached",
        "reason": "turn_has_other_participants",
    }
    controller.command_handler.handle_stop.assert_not_awaited()
    saved = request_store.get_run(owner_run.id)
    assert saved is not None
    assert saved["status"] == "canceled"
    assert saved["callback_status"] == "skipped"
    assert request_store.list_pending_callbacks() == []
    with engine.connect() as conn:
        turn = message_deliveries.get_turn(conn, turn_id)
        steer = message_deliveries.get_delivery(conn, steer_id)
    assert turn is not None
    assert turn["state"] == "active"
    assert turn["control_state"] is None
    assert steer is not None
    assert steer["state"] == "steering"


def test_run_cancel_guard_preserves_an_in_flight_replacement(monkeypatch, tmp_path):
    """Canceling the owner Run cannot supersede another input's P0 replacement."""

    from core.scheduled_tasks import TaskExecutionStore
    from storage.background import attach_agent_run_delivery_in_connection

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session, turn_id = _create_active_test_turn(
        tmp_path,
        native_id="proj_replacement_run_cancel",
    )
    session_id = session["id"]
    request_store = TaskExecutionStore()
    owner_run = request_store.enqueue_agent_run(
        session_id=session_id,
        message="initial owner",
        agent_name="worker",
        callback_session_id="ses_callback",
    )
    assert request_store.claim(owner_run.id) is not None

    with engine.begin() as conn:
        turn = message_deliveries.get_turn(conn, turn_id)
        initial = message_deliveries.delivery_for_turn(conn, turn_id)
        assert turn is not None
        assert initial is not None
        assert attach_agent_run_delivery_in_connection(
            conn,
            owner_run.id,
            session_id=session_id,
            delivery_id=initial["id"],
        )
        replacement = _reserve_submission(
            conn,
            scope_id=session["scope_id"],
            session_id=session_id,
            text="replacement from another user",
        )
        successor_turn_id = message_deliveries.new_turn_id()
        message_deliveries.insert_turn(
            conn,
            turn_id=successor_turn_id,
            session_id=session_id,
            initial_delivery_id=str(replacement["id"]),
            state="waiting",
            backend="claude",
        )
        replacement = message_deliveries.cas_delivery(
            conn,
            str(replacement["id"]),
            expected_version=int(replacement["version"]),
            expected_states=("reserved",),
            values={
                "priority": "p0",
                "state": "interrupt_waiting",
                "turn_id": successor_turn_id,
                "turn_role": "initial",
                "turn_position": 0,
            },
        )
        assert replacement is not None
        controlled = message_deliveries.cas_turn(
            conn,
            turn_id,
            expected_version=int(turn["version"]),
            expected_states=("active",),
            values={
                "control_state": "interrupting",
                "control_mode": "replace",
                "control_attempt_id": message_deliveries.new_attempt_id(),
                "control_expected_native_turn_id": turn["native_turn_id"],
                "control_successor_delivery_id": replacement["id"],
                "control_successor_turn_id": successor_turn_id,
            },
        )
        assert controlled is not None

    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post(
                f"/internal/cancel/{session_id}",
                params={"run_id": owner_run.id},
            )

    response = asyncio.run(_go())

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "session_id": session_id,
        "status": "run_detached",
        "reason": "turn_has_replacement_successor",
    }
    controller.command_handler.handle_stop.assert_not_awaited()
    saved = request_store.get_run(owner_run.id)
    assert saved is not None
    assert saved["status"] == "canceled"
    assert saved["callback_status"] == "skipped"
    with engine.connect() as conn:
        turn = message_deliveries.get_turn(conn, turn_id)
        successor = message_deliveries.get_turn(conn, successor_turn_id)
        replacement = message_deliveries.get_delivery(conn, str(replacement["id"]))
    assert turn is not None
    assert turn["control_mode"] == "replace"
    assert turn["control_successor_turn_id"] == successor_turn_id
    assert successor is not None and successor["state"] == "waiting"
    assert replacement is not None and replacement["state"] == "interrupt_waiting"


def test_run_cancel_retires_its_own_in_flight_replacement(monkeypatch, tmp_path):
    """Canceling a replacement Run cannot leave its waiting prompt activatable."""

    from core.scheduled_tasks import TaskExecutionStore
    from storage.background import attach_agent_run_delivery_in_connection

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session, turn_id = _create_active_test_turn(
        tmp_path,
        native_id="proj_replacement_owner_cancel",
    )
    session_id = session["id"]
    request_store = TaskExecutionStore()
    replacement_run = request_store.enqueue_agent_run(
        session_id=session_id,
        message="replacement from this Run",
        agent_name="worker",
        callback_session_id="ses_callback",
    )
    assert request_store.claim(replacement_run.id) is not None

    with engine.begin() as conn:
        turn = message_deliveries.get_turn(conn, turn_id)
        assert turn is not None
        replacement = _reserve_submission(
            conn,
            scope_id=session["scope_id"],
            session_id=session_id,
            text="replacement from this Run",
        )
        replacement_id = str(replacement["id"])
        successor_turn_id = message_deliveries.new_turn_id()
        message_deliveries.insert_turn(
            conn,
            turn_id=successor_turn_id,
            session_id=session_id,
            initial_delivery_id=replacement_id,
            state="waiting",
            backend="claude",
        )
        replacement = message_deliveries.cas_delivery(
            conn,
            replacement_id,
            expected_version=int(replacement["version"]),
            expected_states=("reserved",),
            values={
                "priority": "p0",
                "state": "interrupt_waiting",
                "turn_id": successor_turn_id,
                "turn_role": "initial",
                "turn_position": 0,
            },
        )
        assert replacement is not None
        assert attach_agent_run_delivery_in_connection(
            conn,
            replacement_run.id,
            session_id=session_id,
            delivery_id=replacement_id,
        )
        controlled = message_deliveries.cas_turn(
            conn,
            turn_id,
            expected_version=int(turn["version"]),
            expected_states=("active",),
            values={
                "control_state": "interrupting",
                "control_mode": "replace",
                "control_attempt_id": message_deliveries.new_attempt_id(),
                "control_expected_native_turn_id": turn["native_turn_id"],
                "control_successor_delivery_id": replacement_id,
                "control_successor_turn_id": successor_turn_id,
            },
        )
        assert controlled is not None

    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post(
                f"/internal/cancel/{session_id}",
                params={"run_id": replacement_run.id},
            )

    response = asyncio.run(_go())

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "session_id": session_id,
        "status": "run_detached",
        "reason": "run_not_owned_by_turn",
    }
    controller.command_handler.handle_stop.assert_not_awaited()
    saved = request_store.get_run(replacement_run.id)
    assert saved is not None
    assert saved["status"] == "canceled"
    assert saved["callback_status"] == "skipped"
    with engine.connect() as conn:
        turn = message_deliveries.get_turn(conn, turn_id)
        successor = message_deliveries.get_turn(conn, successor_turn_id)
        replacement = message_deliveries.get_delivery(conn, replacement_id)
    assert turn is not None
    assert turn["state"] == "active"
    assert turn["control_state"] == "interrupting"
    assert turn["control_mode"] == "stop_only"
    assert turn["control_successor_delivery_id"] is None
    assert turn["control_successor_turn_id"] is None
    assert successor is not None
    assert successor["state"] == "terminal"
    assert successor["terminal_outcome"] == "not_written"
    assert replacement is not None
    assert replacement["state"] == "retired"
    assert replacement["turn_id"] is None


def test_run_cancel_keeps_a_sole_starting_owner_attached(monkeypatch, tmp_path):
    """A claimed initial Run owns its starting Turn until native start resolves."""

    from core.scheduled_tasks import TaskExecutionStore
    from storage.background import attach_agent_run_delivery_in_connection

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session = _create_test_session(
        tmp_path,
        native_id="proj_starting_run_cancel",
    )
    session_id = session["id"]
    request_store = TaskExecutionStore()
    owner_run = request_store.enqueue_agent_run(
        session_id=session_id,
        message="starting owner",
        agent_name="worker",
    )
    assert request_store.claim(owner_run.id) is not None

    with engine.begin() as conn:
        delivery = _reserve_submission(
            conn,
            scope_id=session["scope_id"],
            session_id=session_id,
            text="starting owner",
        )
        turn_id = message_deliveries.new_turn_id()
        message_deliveries.insert_turn(
            conn,
            turn_id=turn_id,
            session_id=session_id,
            initial_delivery_id=str(delivery["id"]),
            state="starting",
            backend="claude",
        )
        claimed = message_deliveries.open_start_attempt(
            conn,
            str(delivery["id"]),
            expected_version=int(delivery["version"]),
            turn_id=turn_id,
            attempt_id=message_deliveries.new_attempt_id(),
        )
        assert claimed is not None and claimed["state"] == "claimed"
        assert attach_agent_run_delivery_in_connection(
            conn,
            owner_run.id,
            session_id=session_id,
            delivery_id=str(claimed["id"]),
        )

    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post(
                f"/internal/cancel/{session_id}",
                params={"run_id": owner_run.id},
            )

    response = asyncio.run(_go())

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "session_id": session_id,
        "status": "cancel_requested",
    }
    controller.command_handler.handle_stop.assert_not_awaited()
    saved = request_store.get_run(owner_run.id)
    assert saved is not None
    assert saved["status"] == "running"
    assert saved["cancel_requested"] is True
    with engine.connect() as conn:
        turn = message_deliveries.get_turn(conn, turn_id)
        delivery = message_deliveries.get_delivery(conn, str(delivery["id"]))
    assert turn is not None
    assert turn["state"] == "starting"
    assert turn["control_state"] == "pending"
    assert turn["control_mode"] == "stop_only"
    assert delivery is not None and delivery["state"] == "claimed"


def test_run_cancel_preserves_shared_starting_batch_siblings(monkeypatch, tmp_path):
    """Detaching one claimed Run replays every surviving batch participant."""

    from core.scheduled_tasks import TaskExecutionStore
    from storage.background import attach_agent_run_delivery_in_connection

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session = _create_test_session(
        tmp_path,
        native_id="proj_shared_starting_run_cancel",
    )
    request_store = TaskExecutionStore()
    runs = [
        request_store.enqueue_agent_run(
            session_id=session["id"],
            message=message,
            agent_name="worker",
        )
        for message in ("canceled batch participant", "surviving batch participant")
    ]
    assert all(request_store.claim(run.id) is not None for run in runs)

    with engine.begin() as conn:
        deliveries = [
            _reserve_submission(
                conn,
                scope_id=session["scope_id"],
                session_id=session["id"],
                text=text,
            )
            for text in ("canceled batch participant", "surviving batch participant")
        ]
        turn_id = message_deliveries.new_turn_id()
        claimed = message_deliveries.claim_start_batch(
            conn,
            turn_id=turn_id,
            session_id=session["id"],
            backend="claude",
            deliveries=deliveries,
            dispatch_text="canceled batch participant\n\nsurviving batch participant",
        )
        for run, delivery in zip(runs, claimed["deliveries"], strict=True):
            assert attach_agent_run_delivery_in_connection(
                conn,
                run.id,
                session_id=session["id"],
                delivery_id=str(delivery["id"]),
            )
        assert message_deliveries.agent_run_exclusively_owns_turn(
            conn,
            run_id=runs[0].id,
            turn_id=turn_id,
        ) == (False, "turn_has_other_participants")

    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    manager = controller.session_turns
    transport = httpx.ASGITransport(app=app)
    dispatched: list[str] = []

    async def _record_start(_session_id, _context, text, **_kwargs):
        dispatched.append(text)

    monkeypatch.setattr(manager, "_run", _record_start)

    async def _go():
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                f"/internal/cancel/{session['id']}",
                params={"run_id": runs[0].id},
            )
        started_original = await manager._start_persisted_turn(
            turn_id,
            context=MessageContext(
                user_id="workbench",
                channel_id=session["id"],
                platform="avibe",
                platform_specific={"workbench_session_id": session["id"]},
            ),
        )
        return response, started_original

    response, started_original = asyncio.run(_go())

    assert response.status_code == 200
    assert response.json()["status"] == "run_detached"
    assert started_original is False
    assert dispatched == ["surviving batch participant"]
    assert request_store.get_run(runs[0].id)["status"] == "canceled"
    assert request_store.get_run(runs[1].id)["status"] == "running"
    with engine.connect() as conn:
        original = message_deliveries.get_turn(conn, turn_id)
        canceled = message_deliveries.get_delivery(
            conn,
            str(claimed["deliveries"][0]["id"]),
        )
        surviving = message_deliveries.get_delivery(
            conn,
            str(claimed["deliveries"][1]["id"]),
        )
    assert original is not None and original["terminal_outcome"] == "not_written"
    assert canceled is not None and canceled["state"] == "retired"
    assert surviving is not None and surviving["state"] == "claimed"
    assert surviving["turn_id"] != turn_id


def test_run_cancel_rechecks_a_changed_current_turn(monkeypatch, tmp_path):
    """A stale observed Turn cannot detach a Run that owns the current Turn."""

    from core.scheduled_tasks import TaskExecutionStore
    from storage.background import attach_agent_run_delivery_in_connection

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session, turn_id = _create_active_test_turn(
        tmp_path,
        native_id="proj_changed_turn_run_cancel",
    )
    session_id = session["id"]
    request_store = TaskExecutionStore()
    owner_run = request_store.enqueue_agent_run(
        session_id=session_id,
        message="recovered current owner",
        agent_name="worker",
    )
    assert request_store.claim(owner_run.id) is not None
    with engine.begin() as conn:
        initial = message_deliveries.delivery_for_turn(conn, turn_id)
        assert initial is not None
        assert attach_agent_run_delivery_in_connection(
            conn,
            owner_run.id,
            session_id=session_id,
            delivery_id=str(initial["id"]),
        )

    controller = _build_controller_double()
    internal_server.create_app(controller)
    context = MessageContext(
        user_id="workbench",
        channel_id=session_id,
        platform="avibe",
        platform_specific={"workbench_session_id": session_id},
    )

    async def _go():
        holder = asyncio.create_task(asyncio.Event().wait())
        controller.session_turns.in_flight[session_id] = session_turns.Turn(
            task=holder,
            context=context,
            logical_turn_id=turn_id,
        )
        try:
            return await controller.session_turns.deliver(
                session_turns.DeliveryRequest(
                    session_id=session_id,
                    priority="p0",
                    content=None,
                    expected_turn_id="trn_recovered_predecessor",
                    expected_exclusive_agent_run_id=owner_run.id,
                ),
                context=context,
            )
        finally:
            holder.cancel()
            await asyncio.gather(holder, return_exceptions=True)

    result = asyncio.run(_go())

    assert result.state == "waiting_terminal"
    controller.command_handler.handle_stop.assert_awaited_once()
    saved = request_store.get_run(owner_run.id)
    assert saved is not None
    assert saved["status"] == "running"
    assert saved["cancel_requested"] is True
    with engine.connect() as conn:
        turn = message_deliveries.get_turn(conn, turn_id)
    assert turn is not None
    assert turn["control_state"] == "waiting_terminal"


def test_run_cancel_without_an_active_turn_settles_atomically(monkeypatch, tmp_path):
    """An orphaned live projection is canceled without inventing a Session Stop."""

    from core.scheduled_tasks import TaskExecutionStore

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _engine, session = _create_test_session(
        tmp_path,
        native_id="proj_ownerless_run_cancel",
    )
    request_store = TaskExecutionStore()
    run = request_store.enqueue_agent_run(
        session_id=session["id"],
        message="orphaned live projection",
        agent_name="worker",
        callback_session_id="ses_callback",
    )
    assert request_store.claim(run.id) is not None

    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post(
                f"/internal/cancel/{session['id']}",
                params={"run_id": run.id},
            )

    response = asyncio.run(_go())

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "session_id": session["id"],
        "status": "run_detached",
        "reason": "run_detached",
    }
    controller.command_handler.handle_stop.assert_not_awaited()
    saved = request_store.get_run(run.id)
    assert saved is not None
    assert saved["status"] == "canceled"
    assert saved["callback_status"] == "skipped"
    assert request_store.list_pending_callbacks() == []


def test_run_cancel_retires_every_pre_native_delivery_atomically(monkeypatch, tmp_path):
    """A canceled Run cannot leave any not-yet-written input eligible to dispatch."""

    from core.inbox_events import bus
    from core.scheduled_tasks import TaskExecutionStore
    from storage.background import attach_agent_run_delivery_in_connection
    from storage.delivery_states import RUN_CANCEL_RETIRE_STATES

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session, turn_id = _create_active_test_turn(
        tmp_path,
        native_id="proj_pre_native_run_cancel",
    )
    session_id = session["id"]
    request_store = TaskExecutionStore()
    runs_and_deliveries: list[tuple[str, str]] = []
    runs_by_state = []
    for delivery_state in RUN_CANCEL_RETIRE_STATES:
        run = request_store.enqueue_agent_run(
            session_id=session_id,
            message=f"cancel {delivery_state} input",
            agent_name="worker",
            callback_session_id="ses_callback",
        )
        assert request_store.claim(run.id) is not None
        runs_by_state.append((delivery_state, run))

    with engine.begin() as conn:
        for delivery_state, run in runs_by_state:
            delivery = _reserve_submission(
                conn,
                scope_id=session["scope_id"],
                session_id=session_id,
                text=f"cancel {delivery_state} input",
            )
            if delivery_state == "queued":
                delivery = message_deliveries.cas_delivery(
                    conn,
                    str(delivery["id"]),
                    expected_version=int(delivery["version"]),
                    expected_states=("reserved",),
                    values={"state": "queued"},
                )
                assert delivery is not None
            elif delivery_state == "pending_steer":
                pending = message_deliveries.open_pending_steer_batch(
                    conn,
                    deliveries=[delivery],
                    turn_id=turn_id,
                    attempt_id=message_deliveries.new_attempt_id(),
                )
                assert len(pending) == 1
                delivery = pending[0]
            else:
                assert delivery_state == "reserved"
            assert attach_agent_run_delivery_in_connection(
                conn,
                run.id,
                session_id=session_id,
                delivery_id=str(delivery["id"]),
            )
            runs_and_deliveries.append((run.id, str(delivery["id"])))

    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        bus,
        "publish",
        lambda event, payload: published.append((event, payload)),
    )
    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return [
                await client.post(
                    f"/internal/cancel/{session_id}",
                    params={"run_id": run_id},
                )
                for run_id, _delivery_id in runs_and_deliveries
            ]

    responses = asyncio.run(_go())

    assert all(response.status_code == 200 for response in responses)
    assert [response.json()["status"] for response in responses] == [
        "run_detached"
    ] * len(RUN_CANCEL_RETIRE_STATES)
    controller.command_handler.handle_stop.assert_not_awaited()
    with engine.connect() as conn:
        retired = [
            message_deliveries.get_delivery(conn, delivery_id)
            for _run_id, delivery_id in runs_and_deliveries
        ]
    assert [row["state"] for row in retired] == [
        "retired"
    ] * len(RUN_CANCEL_RETIRE_STATES)
    assert all(row["current_attempt_id"] is None for row in retired)
    assert all(row["current_target_turn_id"] is None for row in retired)
    for run_id, _delivery_id in runs_and_deliveries:
        saved = request_store.get_run(run_id)
        assert saved is not None
        assert saved["status"] == "canceled"
        assert saved["callback_status"] == "skipped"
    assert request_store.list_pending_callbacks() == []
    assert published.count(("queue.updated", {"session_id": session_id})) == len(
        RUN_CANCEL_RETIRE_STATES
    )


def test_run_cancel_guard_allows_the_sole_initial_run_owner(monkeypatch, tmp_path):
    """A Run remains allowed to stop the backend when it owns the whole Turn."""

    from core.scheduled_tasks import TaskExecutionStore
    from storage.background import attach_agent_run_delivery_in_connection

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session, turn_id = _create_active_test_turn(
        tmp_path,
        native_id="proj_exclusive_run_cancel",
    )
    request_store = TaskExecutionStore()
    run = request_store.enqueue_agent_run(
        session_id=session["id"],
        message="sole initial participant",
        agent_name="worker",
    )
    assert request_store.claim(run.id) is not None

    with engine.begin() as conn:
        initial = message_deliveries.delivery_for_turn(conn, turn_id)
        assert initial is not None
        assert attach_agent_run_delivery_in_connection(
            conn,
            run.id,
            session_id=session["id"],
            delivery_id=initial["id"],
        )
        assert message_deliveries.agent_run_exclusively_owns_turn(
            conn,
            run_id=run.id,
            turn_id=turn_id,
        ) == (True, "exclusive_run_owner")


# --- #84: scheduled provenance survives the merge-queue --------------------------


def _seed_avibe_session_with_queue(queued):
    """Create an isolated Session and seed independent Deliveries oldest first."""
    from core.services import sessions as sessions_service
    from storage import message_deliveries
    from storage.background import attach_agent_run_delivery_in_connection
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import agent_runs
    from storage.settings_service import upsert_scope
    from sqlalchemy import update

    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn, platform="avibe", scope_type="project", native_id="proj_q84", now="2026-05-31T00:00:00Z"
        )
        _seed_project_workdir(conn, scope_id, Path.cwd())
        session = sessions_service.create_session(
            conn, scope_id=scope_id, agent_backend="claude", agent_name="worker"
        )
        for text, prov in queued:
            delivery = message_deliveries.enqueue_queued(
                conn,
                scope_id=scope_id,
                session_id=session["id"],
                platform="avibe",
                author=("harness" if prov is not None else "user"),
                source=("harness" if prov is not None else "user"),
                message_type=("harness" if prov is not None else "user"),
                text=text,
                metadata=({session_turns.SCHEDULED_PROVENANCE_KEY: prov} if prov is not None else None),
                native_message_id=(prov or {}).get("message_id") if prov is not None else None,
            )
            spec = (prov or {}).get("platform_specific") or {}
            run_id = str(spec.get("task_execution_id") or "").strip()
            if run_id and conn.execute(
                select(agent_runs.c.id).where(agent_runs.c.id == run_id)
            ).scalar_one_or_none():
                conn.execute(
                    update(agent_runs)
                    .where(agent_runs.c.id == run_id)
                    .values(session_id=session["id"])
                )
                assert attach_agent_run_delivery_in_connection(
                    conn,
                    run_id,
                    session_id=session["id"],
                    delivery_id=str(delivery["id"]),
                )
    return session["id"]


def _manager_capturing_runs():
    """A SessionTurnManager whose ``_run`` records each flushed turn's (text, source,
    suppress_delivery) instead of dispatching."""
    runs: list = []
    mgr = session_turns.SessionTurnManager(
        controller=types.SimpleNamespace(),
        build_context=lambda sid: MessageContext(
            user_id="U", channel_id="C", platform="avibe", platform_specific={"agent_session_id": sid}
        ),
    )

    async def _fake_run(sid, context, text, *, source=SOURCE_HUMAN, **_kwargs):
        runs.append((text, source, context))

    mgr._run = _fake_run
    return mgr, runs


def _manager_accepting_runs():
    """Capture durable starts while simulating exact native acceptance."""

    manager, runs = _manager_capturing_runs()

    async def _accept(
        sid,
        context,
        text,
        *,
        source=SOURCE_HUMAN,
        logical_turn_id=None,
        delivery_id=None,
        **_kwargs,
    ):
        runs.append((text, source, context))
        _bind_test_native_start(manager._sqlite_engine(), context)
        with manager._sqlite_engine().begin() as conn:
            accepted = message_deliveries.materialize_start_acceptance(
                conn,
                turn_id=logical_turn_id,
                evidence={"kind": "test_native_acceptance"},
            )
            assert accepted
        manager._publish_materialized_delivery(delivery_id)
        settled = manager._terminalize_durable_turn(
            logical_turn_id,
            "completed",
            settled_by="test",
            evidence_kind="test_native_terminal",
        )
        assert settled["changed"] is True

    manager._run = _accept
    return manager, runs


def test_flush_runs_authorized_remote_fifo_head(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session = _create_test_session(
        tmp_path,
        native_id="proj_remote_queue_retirement",
    )
    _seed_remote_worker()
    with engine.begin() as conn:
        remote = message_deliveries.enqueue_queued(
            conn,
            scope_id=session["scope_id"],
            session_id=session["id"],
            text="remote queued input",
            metadata=_authorized_remote_message_metadata(),
            author_id="remote-user",
            message_kind="original",
        )
        local = message_deliveries.enqueue_queued(
            conn,
            scope_id=session["scope_id"],
            session_id=session["id"],
            text="local queued input",
            author_id="local",
            message_kind="original",
        )

    manager, runs = _manager_accepting_runs()
    assert asyncio.run(manager.flush_queue(session["id"])) is True

    assert [(text, source) for text, source, _context in runs] == [
        ("remote queued input", SOURCE_HUMAN),
    ]
    with engine.connect() as conn:
        remote_saved = message_deliveries.get_delivery(conn, remote["id"])
        local_saved = message_deliveries.get_delivery(conn, local["id"])
    assert remote_saved is not None
    assert remote_saved["state"] == "accepted"
    assert local_saved is not None
    assert local_saved["state"] == "claimed"


def test_claimed_authorized_remote_turn_reaches_native_dispatch(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine, session = _create_test_session(
        tmp_path,
        native_id="proj_remote_claimed_turn",
    )
    _seed_remote_worker()
    turn_id = message_deliveries.new_turn_id()
    with engine.begin() as conn:
        queued = message_deliveries.enqueue_queued(
            conn,
            scope_id=session["scope_id"],
            session_id=session["id"],
            text="claimed remote input",
            metadata=_authorized_remote_message_metadata(),
        )
        row = message_deliveries.get_delivery(conn, queued["id"])
        assert row is not None
        message_deliveries.claim_start_batch(
            conn,
            turn_id=turn_id,
            session_id=session["id"],
            backend="claude",
            deliveries=[row],
            dispatch_text="claimed remote input",
        )

    manager = session_turns.SessionTurnManager(
        controller=types.SimpleNamespace(),
        build_context=lambda sid: MessageContext(
            user_id="U",
            channel_id="C",
            platform="avibe",
            platform_specific={"agent_session_id": sid},
        ),
    )

    runs = []

    async def capture_run(_session_id, _context, text, **_kwargs):
        runs.append(text)

    manager._run = capture_run
    assert asyncio.run(manager._start_persisted_turn(turn_id)) is True
    assert runs == ["claimed remote input"]

    with engine.connect() as conn:
        saved_turn = message_deliveries.get_turn(conn, turn_id)
        saved_delivery = message_deliveries.get_delivery(conn, queued["id"])
    assert saved_turn is not None
    assert saved_turn["state"] == "starting"
    assert saved_delivery is not None
    assert saved_delivery["state"] == "claimed"
def test_flush_runs_scheduled_row_as_scheduled_with_provenance(tmp_path, monkeypatch):
    """A queued scheduled run flushes as its OWN SOURCE_SCHEDULED turn with its
    delivery provenance restored — not merged into a plain user turn (#84)."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    override = {"channel_id": "slack-321", "platform": "slack"}
    session_id = _seed_avibe_session_with_queue(
        [(
            "scheduled prompt",
            {
                "message_id": "scheduled:exec-1",
                "platform_specific": {
                    "suppress_delivery": True,
                    "delivery_override": override,
                    "task_trigger_kind": "task",
                },
            },
        )]
    )
    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    assert len(runs) == 1
    text, source, ctx = runs[0]
    # Ran as scheduled (not user), with the FULL provenance restored: the delivery
    # override (the redirect _get_target_context uses) + suppress_delivery (#84 / P1)
    # AND the stable scheduled native id for dedup (P2).
    assert (text, source) == ("scheduled prompt", SOURCE_SCHEDULED)
    assert ctx.platform_specific["suppress_delivery"] is True
    assert ctx.platform_specific["delivery_override"] == override
    assert ctx.message_id == "scheduled:exec-1"

    from storage import messages_service
    from storage.db import create_sqlite_engine

    with create_sqlite_engine().begin() as conn:
        assert message_deliveries.list_queued(conn, session_id) == []


def test_flush_merges_compatible_scheduled_deliveries_into_one_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    def prov(execution_id: str, message_id: str = "watch:def-watch") -> dict:
        return {
            "message_id": message_id,
            "platform_specific": {
                "task_execution_id": execution_id,
                "task_trigger_kind": "watch",
                "task_definition_id": "def-watch",
            },
        }

    session_id = _seed_avibe_session_with_queue(
        [
            ("first callback", prov("run-1", "watch:def-watch:run-1")),
            ("second callback", prov("run-2", "watch:def-watch:run-2")),
            ("third callback", prov("run-3", "watch:def-watch:run-3")),
        ]
    )

    from storage.db import create_sqlite_engine

    with create_sqlite_engine().begin() as conn:
        assert [row["text"] for row in message_deliveries.list_queued(conn, session_id)] == [
            "first callback",
            "second callback",
            "third callback",
        ]

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    assert len(runs) == 1
    text, source, ctx = runs[0]
    assert source == SOURCE_SCHEDULED
    assert text == "first callback\n\n---\n\nsecond callback\n\n---\n\nthird callback"
    assert ctx.platform_specific["task_definition_id"] == "def-watch"
    assert len(ctx.platform_specific["delivery_ids"]) == 3
    with create_sqlite_engine().begin() as conn:
        assert message_deliveries.list_queued(conn, session_id) == []


def test_flush_claims_compatible_agent_run_deliveries_as_one_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    from core.scheduled_tasks import TaskExecutionStore
    from storage.background import SQLiteBackgroundTaskStore
    from storage.db import create_sqlite_engine

    request_store = TaskExecutionStore()

    def prov(execution_id: str) -> dict:
        return {
            "message_id": f"agent_run:{execution_id}",
            "platform_specific": {
                "task_execution_id": execution_id,
                "task_trigger_kind": "agent_run",
                "task_definition_id": None,
                "vibe_agent_name": "codex",
                "source_kind": None,
                "callback_session_id": None,
            },
        }

    queued: list[tuple[str, dict]] = []
    for index in range(4):
        request = request_store.enqueue_agent_run(
            session_id="placeholder",
            message=f"cli agent message {index + 1}",
            agent_name="codex",
        )
        assert request_store.claim(request.id) is not None
        request_store.requeue(request.id)
        queued.append((f"cli agent message {index + 1}", prov(request.id)))

    session_id = _seed_avibe_session_with_queue(queued)

    from storage.db import create_sqlite_engine

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    assert len(runs) == 1
    text, source, ctx = runs[0]
    assert source == SOURCE_SCHEDULED
    assert text == (
        "cli agent message 1\n\n---\n\ncli agent message 2\n\n---\n\n"
        "cli agent message 3\n\n---\n\ncli agent message 4"
    )
    execution_ids = [item[1]["platform_specific"]["task_execution_id"] for item in queued]
    assert ctx.platform_specific["task_execution_id"] == execution_ids[0]
    assert ctx.platform_specific["accepted_agent_run_ids"] == execution_ids

    bg = SQLiteBackgroundTaskStore()
    try:
        stored = {run_id: bg.get_run(run_id) for run_id in execution_ids}
    finally:
        bg.close()

    assert [stored[run_id]["status"] for run_id in execution_ids] == ["running"] * 4
    assert len({stored[run_id]["delivery_id"] for run_id in execution_ids}) == 4
    assert all(stored[run_id]["delivery_id"] for run_id in execution_ids)
    assert all(
        "workbench_queue_holds_run" not in stored[run_id]["metadata"]
        for run_id in execution_ids
    )
    with create_sqlite_engine().connect() as conn:
        assert message_deliveries.list_queued(conn, session_id) == []


def test_flush_background_agent_run_preserves_primary_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    from core.scheduled_tasks import TaskExecutionStore
    from storage.db import create_sqlite_engine
    from storage.models import messages

    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id="placeholder",
        message="background prompt",
        agent_name="codex",
    )
    assert request_store.claim(request.id) is not None
    request_store.requeue(request.id)
    session_id = _seed_avibe_session_with_queue(
        [
            (
                "background prompt",
                {
                    "message_id": f"agent_run:{request.id}",
                    "platform_specific": {
                        "task_execution_id": request.id,
                        "task_trigger_kind": "agent_run",
                        "task_definition_id": None,
                        "vibe_agent_name": "codex",
                        "suppress_delivery": True,
                    },
                },
            )
        ]
    )
    mgr, runs = _manager_capturing_runs()

    async def _accept_run(
        sid,
        context,
        text,
        *,
        source=SOURCE_HUMAN,
        logical_turn_id=None,
        delivery_id=None,
        **_kwargs,
    ):
        _bind_test_native_start(mgr._sqlite_engine(), context)
        with mgr._sqlite_engine().begin() as conn:
            accepted = message_deliveries.materialize_start_acceptance(
                conn,
                turn_id=logical_turn_id,
                evidence={"kind": "test_native_acceptance"},
            )
            assert accepted
        runs.append((text, source, context))

    mgr._run = _accept_run

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    assert len(runs) == 1
    with create_sqlite_engine().connect() as conn:
        rows = conn.execute(
            select(messages).where(messages.c.session_id == session_id)
        ).mappings().all()

    harness_rows = [row for row in rows if row["type"] == "harness"]
    assert [(row["native_message_id"], row["content_text"]) for row in harness_rows] == [
        (f"agent_run:{request.id}", "background prompt")
    ]
    assert len(rows) == 1


def test_cancel_retires_exact_agent_run_delivery_without_reordering_survivors(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    from core.scheduled_tasks import TaskExecutionStore
    from storage.background import SQLiteBackgroundTaskStore

    request_store = TaskExecutionStore()

    def prov(execution_id: str) -> dict:
        return {
            "message_id": f"agent_run:{execution_id}",
            "platform_specific": {
                "task_execution_id": execution_id,
                "task_trigger_kind": "agent_run",
                "task_definition_id": None,
                "vibe_agent_name": "codex",
                "source_kind": None,
                "callback_session_id": None,
            },
        }

    queued: list[tuple[str, dict]] = []
    run_ids: list[str] = []
    for index in range(3):
        request = request_store.enqueue_agent_run(
            session_id="placeholder",
            message=f"cli agent message {index + 1}",
            agent_name="codex",
        )
        assert request_store.claim(request.id) is not None
        request_store.requeue(request.id)
        queued.append((f"cli agent message {index + 1}", prov(request.id)))
        run_ids.append(request.id)

    session_id = _seed_avibe_session_with_queue(queued)

    from storage.db import create_sqlite_engine

    from core.inbox_events import bus

    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(bus, "publish", lambda event, payload: published.append((event, payload)))

    bg = SQLiteBackgroundTaskStore()
    try:
        assert bg.cancel_run(run_ids[1]) is True
    finally:
        bg.close()

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    assert len(runs) == 1
    text, source, ctx = runs[0]
    assert source == SOURCE_SCHEDULED
    assert text == "cli agent message 1\n\n---\n\ncli agent message 3"
    assert ctx.platform_specific["task_execution_id"] == run_ids[0]
    assert ctx.platform_specific["accepted_agent_run_ids"] == [
        run_ids[0],
        run_ids[2],
    ]

    with create_sqlite_engine().begin() as conn:
        assert message_deliveries.list_queued(conn, session_id) == []

    bg = SQLiteBackgroundTaskStore()
    try:
        stored = {run_id: bg.get_run(run_id) for run_id in run_ids}
    finally:
        bg.close()

    assert stored[run_ids[0]]["status"] == "running"
    assert stored[run_ids[1]]["status"] == "canceled"
    assert stored[run_ids[2]]["status"] == "running"
    assert len({stored[run_ids[0]]["delivery_id"], stored[run_ids[2]]["delivery_id"]}) == 2
    with create_sqlite_engine().connect() as conn:
        canceled_delivery = message_deliveries.get_delivery(
            conn,
            stored[run_ids[1]]["delivery_id"],
        )
    assert canceled_delivery is not None
    assert canceled_delivery["state"] == "retired"
    assert published.count(("queue.updated", {"session_id": session_id})) >= 1


def test_turn_claim_retires_terminal_agent_run_and_dispatches_the_rest(tmp_path, monkeypatch):
    """A Run terminalized behind its Delivery's back must not brick the flush.

    ``cancel_run`` retires the Delivery itself, but a Run can also settle without
    that path — a watch fire whose Run ended ``failed``, or a crash between the
    two writes. The claim path then converges on the same outcome as the proper
    cancellation above instead of raising and crash-looping the controller.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    from core.scheduled_tasks import TaskExecutionStore
    from storage.background import SQLiteBackgroundTaskStore
    from storage.db import create_sqlite_engine
    from storage.models import agent_runs
    from sqlalchemy import update

    request_store = TaskExecutionStore()

    def prov(execution_id: str) -> dict:
        return {
            "message_id": f"agent_run:{execution_id}",
            "platform_specific": {
                "task_execution_id": execution_id,
                "task_trigger_kind": "agent_run",
                "task_definition_id": None,
                "vibe_agent_name": "codex",
                "source_kind": None,
                "callback_session_id": None,
            },
        }

    queued: list[tuple[str, dict]] = []
    run_ids: list[str] = []
    for index in range(3):
        request = request_store.enqueue_agent_run(
            session_id="placeholder",
            message=f"cli agent message {index + 1}",
            agent_name="codex",
        )
        assert request_store.claim(request.id) is not None
        request_store.requeue(request.id)
        queued.append((f"cli agent message {index + 1}", prov(request.id)))
        run_ids.append(request.id)

    session_id = _seed_avibe_session_with_queue(queued)
    with create_sqlite_engine().begin() as conn:
        conn.execute(
            update(agent_runs)
            .where(agent_runs.c.id == run_ids[1])
            .values(
                status="canceled",
                cancel_requested=1,
                completed_at="2026-06-22T00:00:10Z",
            )
        )

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    assert len(runs) == 1
    text, _source, ctx = runs[0]
    assert text == "cli agent message 1\n\n---\n\ncli agent message 3"
    assert ctx.platform_specific["accepted_agent_run_ids"] == [
        run_ids[0],
        run_ids[2],
    ]

    bg = SQLiteBackgroundTaskStore()
    try:
        stored = {run_id: bg.get_run(run_id) for run_id in run_ids}
    finally:
        bg.close()

    assert stored[run_ids[0]]["status"] == "running"
    assert stored[run_ids[1]]["status"] == "canceled"
    assert stored[run_ids[2]]["status"] == "running"
    with mgr._sqlite_engine().connect() as conn:
        assert message_deliveries.list_queued(conn, session_id) == []
        retired = message_deliveries.get_delivery(
            conn,
            stored[run_ids[1]]["delivery_id"],
        )
    assert retired is not None
    assert retired["state"] == "retired"


def test_flush_merges_agent_runs_with_different_callback_targets(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    from core.scheduled_tasks import TaskExecutionStore

    request_store = TaskExecutionStore()

    def prov(execution_id: str, callback_session_id: str) -> dict:
        return {
            "message_id": f"agent_run:{execution_id}",
            "platform_specific": {
                "task_execution_id": execution_id,
                "task_trigger_kind": "agent_run",
                "task_definition_id": None,
                "vibe_agent_name": "codex",
                "callback_session_id": callback_session_id,
            },
        }

    queued: list[tuple[str, dict]] = []
    for index, callback_session_id in enumerate(["caller-a", "caller-b"]):
        request = request_store.enqueue_agent_run(
            session_id="placeholder",
            message=f"cli agent message {index + 1}",
            agent_name="codex",
            callback_session_id=callback_session_id,
        )
        assert request_store.claim(request.id) is not None
        request_store.requeue(request.id)
        queued.append((f"cli agent message {index + 1}", prov(request.id, callback_session_id)))

    session_id = _seed_avibe_session_with_queue(queued)

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    assert len(runs) == 1
    text, source, ctx = runs[0]
    assert source == SOURCE_SCHEDULED
    assert text == "cli agent message 1\n\n---\n\ncli agent message 2"
    assert len(ctx.platform_specific["accepted_agent_run_ids"]) == 2
    assert "coalesced_queue" not in ctx.platform_specific
    with mgr._sqlite_engine().connect() as conn:
        assert message_deliveries.list_queued(conn, session_id) == []


def test_flush_merges_callback_backed_agent_runs_by_delivery_compatibility(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    from core.scheduled_tasks import TaskExecutionStore

    request_store = TaskExecutionStore()

    def prov(execution_id: str) -> dict:
        return {
            "message_id": f"agent_run:{execution_id}",
            "platform_specific": {
                "task_execution_id": execution_id,
                "task_trigger_kind": "agent_run",
                "task_definition_id": None,
                "vibe_agent_name": "codex",
                "callback_session_id": "same-caller",
            },
        }

    queued: list[tuple[str, dict]] = []
    for index in range(2):
        request = request_store.enqueue_agent_run(
            session_id="placeholder",
            message=f"callback agent message {index + 1}",
            agent_name="codex",
            callback_session_id="same-caller",
        )
        assert request_store.claim(request.id) is not None
        request_store.requeue(request.id)
        queued.append((f"callback agent message {index + 1}", prov(request.id)))

    session_id = _seed_avibe_session_with_queue(queued)

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    assert len(runs) == 1
    text, source, ctx = runs[0]
    assert source == SOURCE_SCHEDULED
    assert text == "callback agent message 1\n\n---\n\ncallback agent message 2"
    assert len(ctx.platform_specific["accepted_agent_run_ids"]) == 2
    assert "coalesced_queue" not in ctx.platform_specific


def test_flush_merges_agent_runs_without_redundant_provenance_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    from core.scheduled_tasks import TaskExecutionStore

    request_store = TaskExecutionStore()

    def prov(execution_id: str) -> dict:
        return {
            "message_id": f"agent_run:{execution_id}",
            "platform_specific": {
                "task_execution_id": execution_id,
                "task_trigger_kind": "agent_run",
                "task_definition_id": None,
                "vibe_agent_name": "codex",
            },
        }

    queued: list[tuple[str, dict]] = []
    for index in range(2):
        request = request_store.enqueue_agent_run(
            session_id="placeholder",
            message=f"legacy callback message {index + 1}",
            agent_name="codex",
            callback_session_id=f"caller-{index + 1}",
        )
        assert request_store.claim(request.id) is not None
        request_store.requeue(request.id)
        queued.append((f"legacy callback message {index + 1}", prov(request.id)))

    session_id = _seed_avibe_session_with_queue(queued)

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    assert len(runs) == 1
    text, source, ctx = runs[0]
    assert source == SOURCE_SCHEDULED
    assert text == "legacy callback message 1\n\n---\n\nlegacy callback message 2"
    assert len(ctx.platform_specific["accepted_agent_run_ids"]) == 2
    assert "coalesced_queue" not in ctx.platform_specific


def test_flush_does_not_claim_agent_runs_when_context_build_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    from core.scheduled_tasks import TaskExecutionStore
    from storage.background import SQLiteBackgroundTaskStore

    request_store = TaskExecutionStore()

    def prov(execution_id: str) -> dict:
        return {
            "message_id": f"agent_run:{execution_id}",
            "platform_specific": {
                "task_execution_id": execution_id,
                "task_trigger_kind": "agent_run",
                "task_definition_id": None,
                "vibe_agent_name": "codex",
                "source_kind": None,
                "callback_session_id": None,
            },
        }

    queued: list[tuple[str, dict]] = []
    run_ids: list[str] = []
    for index in range(2):
        request = request_store.enqueue_agent_run(
            session_id="placeholder",
            message=f"cli agent message {index + 1}",
            agent_name="codex",
        )
        assert request_store.claim(request.id) is not None
        request_store.requeue(request.id)
        queued.append((f"cli agent message {index + 1}", prov(request.id)))
        run_ids.append(request.id)

    session_id = _seed_avibe_session_with_queue(queued)

    mgr = session_turns.SessionTurnManager(
        controller=types.SimpleNamespace(),
        build_context=lambda _sid: (_ for _ in ()).throw(RuntimeError("context unavailable")),
    )

    assert asyncio.run(mgr.flush_queue(session_id)) is False

    from storage.db import create_sqlite_engine

    with create_sqlite_engine().begin() as conn:
        queued_rows = message_deliveries.list_queued(conn, session_id)
    assert [row["text"] for row in queued_rows] == ["cli agent message 1", "cli agent message 2"]

    bg = SQLiteBackgroundTaskStore()
    try:
        stored = {run_id: bg.get_run(run_id) for run_id in run_ids}
    finally:
        bg.close()

    assert {row["status"] for row in stored.values()} == {"running"}
    assert all(row["delivery_id"] for row in stored.values())


def test_flush_quarantines_agent_run_when_native_start_may_have_written(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    from core.scheduled_tasks import TaskExecutionStore
    from storage.background import SQLiteBackgroundTaskStore

    request_store = TaskExecutionStore()

    def prov(execution_id: str) -> dict:
        return {
            "message_id": f"agent_run:{execution_id}",
            "platform_specific": {
                "task_execution_id": execution_id,
                "task_trigger_kind": "agent_run",
                "task_definition_id": None,
                "vibe_agent_name": "codex",
            },
        }

    queued: list[tuple[str, dict]] = []
    run_ids: list[str] = []
    for index in range(2):
        request = request_store.enqueue_agent_run(
            session_id="placeholder",
            message=f"cli agent message {index + 1}",
            agent_name="codex",
        )
        assert request_store.claim(request.id) is not None
        request_store.requeue(request.id)
        queued.append((f"cli agent message {index + 1}", prov(request.id)))
        run_ids.append(request.id)

    session_id = _seed_avibe_session_with_queue(queued)
    mgr, _runs = _manager_capturing_runs()

    async def _failing_run(*_args, **_kwargs):
        raise RuntimeError("dispatch did not start")

    mgr._run = _failing_run

    assert asyncio.run(mgr.flush_queue(session_id)) is False

    from storage.db import create_sqlite_engine

    with create_sqlite_engine().begin() as conn:
        queued_rows = message_deliveries.list_queued(conn, session_id)
        unresolved_turn = message_deliveries.active_turn(conn, session_id)
        assert unresolved_turn is not None
        unresolved_batch = message_deliveries.initial_deliveries_for_turn(
            conn,
            unresolved_turn["id"],
        )
    assert queued_rows == []
    assert unresolved_turn["start_receipt_outcome"] == "unknown"
    assert [row["state"] for row in unresolved_batch] == ["claimed", "claimed"]

    bg = SQLiteBackgroundTaskStore()
    try:
        stored = {run_id: bg.get_run(run_id) for run_id in run_ids}
    finally:
        bg.close()

    assert [stored[run_id]["status"] for run_id in run_ids] == ["running", "running"]
    assert len({stored[run_id]["delivery_id"] for run_id in run_ids}) == 2


def test_flush_suppressed_segment_claims_each_delivery_id_in_one_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    def prov(execution_id: str, instruction: str) -> dict:
        return {
            "message_id": f"watch:def-watch:{execution_id}",
            "platform_specific": {
                "task_execution_id": execution_id,
                "task_trigger_kind": "watch",
                "task_definition_id": "def-watch",
                "harness_display_prompt": instruction,
                "suppress_delivery": True,
            },
        }

    session_id = _seed_avibe_session_with_queue(
        [
            ("first callback", prov("run-1", "watch the deploy")),
            ("second callback", prov("run-2", "watch the deploy and page me")),
        ]
    )

    from storage.db import create_sqlite_engine

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    assert len(runs) == 1
    assert runs[0][2].message_id == "watch:def-watch:run-1"
    assert len(runs[0][2].platform_specific["delivery_ids"]) == 2
    # Every merged Delivery's display snapshot travels with the context. The dispatch
    # text carries BOTH prompts, so a consumer that reads the singular ``display_text``
    # (the IM prompt echo) would announce one instruction for a two-prompt result.
    assert runs[0][2].platform_specific["display_texts"] == [
        "first callback",
        "second callback",
    ]
    # Same for the composed kinds, whose echo shows the stored instruction instead of
    # the snapshot: an instruction edited between the two firings leaves the merged
    # batch dispatching both, so each Delivery's own stamped instruction travels too.
    assert runs[0][2].platform_specific["harness_display_prompts"] == [
        "watch the deploy",
        "watch the deploy and page me",
    ]
    with create_sqlite_engine().begin() as conn:
        assert message_deliveries.list_queued(conn, session_id) == []


def test_flush_does_not_coalesce_scheduled_callbacks_with_different_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    def prov(execution_id: str, channel_id: str) -> dict:
        return {
            "message_id": f"watch:def-watch:{execution_id}",
            "platform_specific": {
                "task_execution_id": execution_id,
                "task_trigger_kind": "watch",
                "task_definition_id": "def-watch",
                "delivery_override": {"platform": "slack", "channel_id": channel_id},
            },
        }

    session_id = _seed_avibe_session_with_queue(
        [
            ("first callback", prov("run-1", "C1")),
            ("different delivery", prov("run-2", "C2")),
        ]
    )

    from storage import messages_service
    from storage.db import create_sqlite_engine

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    assert [(text, source) for text, source, _ in runs] == [("first callback", SOURCE_SCHEDULED)]
    with create_sqlite_engine().begin() as conn:
        remaining = message_deliveries.list_queued(conn, session_id)
    assert [row["text"] for row in remaining] == ["different delivery"]


def test_flush_does_not_coalesce_scheduled_callbacks_with_different_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    def prov(execution_id: str, agent_name: str) -> dict:
        return {
            "message_id": f"watch:def-watch:{execution_id}",
            "platform_specific": {
                "task_execution_id": execution_id,
                "task_trigger_kind": "watch",
                "task_definition_id": "def-watch",
                "vibe_agent_name": agent_name,
            },
        }

    session_id = _seed_avibe_session_with_queue(
        [
            ("codex callback", prov("run-1", "codex")),
            ("claude callback", prov("run-2", "claude")),
        ]
    )

    from storage import messages_service
    from storage.db import create_sqlite_engine

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    text, source, ctx = runs[0]
    assert (text, source) == ("codex callback", SOURCE_SCHEDULED)
    assert "vibe_agent_name" not in ctx.platform_specific
    with create_sqlite_engine().begin() as conn:
        remaining = message_deliveries.list_queued(conn, session_id)
    assert [row["text"] for row in remaining] == ["claude callback"]


def test_capture_scheduled_target_agent_splits_coalescing_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    def captured_prov(execution_id: str, agent_name: str) -> dict:
        ctx = MessageContext(
            user_id="U",
            channel_id="C",
            platform="avibe",
            message_id=f"watch:def-watch:{execution_id}",
            platform_specific={
                "task_execution_id": execution_id,
                "task_trigger_kind": "watch",
                "task_definition_id": "def-watch",
                "agent_session_target": {"id": f"ses-{agent_name}", "agent_name": agent_name},
            },
        )
        return session_turns.capture_scheduled_provenance(ctx)

    session_id = _seed_avibe_session_with_queue(
        [
            ("codex target callback", captured_prov("run-1", "codex")),
            ("claude target callback", captured_prov("run-2", "claude")),
        ]
    )

    from storage import messages_service
    from storage.db import create_sqlite_engine

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    text, source, ctx = runs[0]
    assert (text, source) == ("codex target callback", SOURCE_SCHEDULED)
    assert session_turns.SCHEDULED_TARGET_AGENT_KEY not in ctx.platform_specific
    with create_sqlite_engine().begin() as conn:
        remaining = message_deliveries.list_queued(conn, session_id)
    assert [row["text"] for row in remaining] == ["claude target callback"]


def test_flush_preserves_scheduled_prompt_whitespace(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    session_id = _seed_avibe_session_with_queue(
        [
            (
                "  indented: true\n",
                {
                    "message_id": "watch:def-watch:run-1",
                    "platform_specific": {
                        "task_execution_id": "run-1",
                        "task_trigger_kind": "watch",
                        "task_definition_id": "def-watch",
                    },
                },
            )
        ]
    )

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    assert runs[0][0] == "  indented: true\n"


def test_flush_does_not_mark_native_ids_when_context_build_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    session_id = _seed_avibe_session_with_queue(
        [
            (
                "suppressed callback",
                {
                    "message_id": "watch:def-watch:run-1",
                    "platform_specific": {
                        "task_execution_id": "run-1",
                        "task_trigger_kind": "watch",
                        "task_definition_id": "def-watch",
                        "suppress_delivery": True,
                    },
                },
            )
        ]
    )

    from storage.db import create_sqlite_engine

    mgr = session_turns.SessionTurnManager(
        controller=types.SimpleNamespace(),
        build_context=lambda sid: (_ for _ in ()).throw(RuntimeError("missing session")),
    )

    assert asyncio.run(mgr.flush_queue(session_id)) is False
    with create_sqlite_engine().begin() as conn:
        queued = message_deliveries.list_queued(conn, session_id)
    assert [row["native_message_id"] for row in queued] == ["watch:def-watch:run-1"]


def test_capture_scheduled_provenance_keeps_delivery_drops_routing():
    """capture_scheduled_provenance keeps the delivery / attribution keys — notably
    delivery_override, the redirect MessageDispatcher._get_target_context uses — and
    DROPS the routing keys the flush rebuilds, so a queued scheduled run keeps its
    delivery target (#84 / Codex P1 #3338692433)."""
    override = {"channel_id": "slack-9", "platform": "slack"}
    ctx = MessageContext(
        user_id="U",
        channel_id="C",
        platform="avibe",
        message_id="scheduled:exec-9",
        platform_specific={
            "platform": "avibe",
            "is_dm": False,
            "agent_session_id": "ses1",
            "agent_session_target": {"id": "ses1", "agent_name": "worker"},
            "delivery_override": override,
            "suppress_delivery": True,
            "turn_source": "scheduled",
            "task_trigger_kind": "task",
        },
    )
    prov = session_turns.capture_scheduled_provenance(ctx)
    # The stable native id is captured for dedup (Codex P2).
    assert prov["message_id"] == "scheduled:exec-9"
    spec = prov["platform_specific"]
    # Delivery / attribution provenance kept.
    assert spec["delivery_override"] == override
    assert spec["suppress_delivery"] is True
    assert spec["turn_source"] == "scheduled"
    assert spec["task_trigger_kind"] == "task"
    assert spec[session_turns.SCHEDULED_TARGET_AGENT_KEY] == "worker"
    # Routing keys the flush rebuilds are NOT carried.
    for routing in ("platform", "is_dm", "agent_session_id", "agent_session_target"):
        assert routing not in spec


def test_boot_publishes_app_then_waits_for_controller_recovery(
    monkeypatch,
    tmp_path,
):
    """HFR-152: HTTP serving waits for controller-owned runtime recovery."""
    import uvicorn

    calls: list[str] = []
    app_created = asyncio.Event()
    recovery_complete = asyncio.Event()
    manager = SimpleNamespace(
        recover_durable_delivery_state=AsyncMock(),
        recover_persisted_agent_run_queue=AsyncMock(),
    )
    controller = SimpleNamespace(
        session_turns=manager,
        _delivery_recovery_complete=recovery_complete,
    )

    class _Server:
        def __init__(self, _config):
            pass

        async def serve(self, *, sockets):
            assert len(sockets) == 1
            assert recovery_complete.is_set()
            calls.append("serve")

    listener = SimpleNamespace(close=lambda: calls.append("close"))

    def _create_app(_controller):
        calls.append("app")
        app_created.set()
        return object()

    monkeypatch.setattr(internal_server, "create_app", _create_app)
    monkeypatch.setattr(
        internal_server,
        "_bind_socket",
        lambda _path: (listener, tmp_path / "internal.sock"),
    )
    monkeypatch.setattr(uvicorn, "Config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(uvicorn, "Server", _Server)

    async def _run() -> None:
        serving = asyncio.create_task(internal_server.serve(controller))
        await app_created.wait()
        assert calls == ["app"]
        manager.recover_durable_delivery_state.assert_not_awaited()
        manager.recover_persisted_agent_run_queue.assert_not_awaited()
        recovery_complete.set()
        await serving

    asyncio.run(_run())

    assert calls == ["app", "serve", "close"]


def _seed_slack_dm_session(conn, tmp_path, *, dm_chat_id: str, user_id: str = "U_DM"):
    """Create a Slack DM Session whose scope_id is the USER id, like production."""

    import json as _json

    from core.services import sessions as sessions_service
    from storage.models import scope_settings
    from storage.settings_service import upsert_scope

    now = "2026-08-09T00:00:00Z"
    scope_id = upsert_scope(
        conn,
        platform="slack",
        scope_type="user",
        native_id=user_id,
        now=now,
    )
    payload = {"bound_at": now}
    if dm_chat_id:
        payload["dm_chat_id"] = dm_chat_id
    conn.execute(
        scope_settings.insert().values(
            scope_id=scope_id,
            enabled=1,
            role=None,
            workdir=str(tmp_path),
            agent_name=None,
            agent_backend=None,
            agent_variant=None,
            model=None,
            reasoning_effort=None,
            require_mention=None,
            settings_version=1,
            settings_json=_json.dumps(payload),
            created_at=now,
            updated_at=now,
        )
    )
    return sessions_service.create_session(
        conn,
        scope_id=scope_id,
        agent_backend="claude",
        agent_name="claude",
    )


def test_build_session_context_uses_bound_dm_channel_for_dm_scope(monkeypatch, tmp_path):
    """A DM Session's scope_id is the USER id, which is not a channel.

    Slack's ``chat.postMessage`` tolerates a user id (it opens the DM), so sending
    kept working — but ``reactions.add`` answers ``channel_not_found``, so the
    reaction ack failed and silently downgraded to the ack message. The builder
    must swap in the bound ``dm_chat_id`` like every other resolver does.
    """

    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        session = _seed_slack_dm_session(conn, tmp_path, dm_chat_id="D_REAL")

    context = internal_server._build_session_context(session["id"])

    assert context.platform == "slack"
    assert context.user_id == "U_DM"
    assert context.channel_id == "D_REAL"
    assert context.platform_specific["is_dm"] is True


def test_build_session_context_falls_back_to_scope_id_without_dm_binding(monkeypatch, tmp_path):
    """No recorded dm_chat_id -> keep the old behaviour rather than inventing one."""

    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        session = _seed_slack_dm_session(conn, tmp_path, dm_chat_id="", user_id="U_UNBOUND")

    context = internal_server._build_session_context(session["id"])

    assert context.channel_id == "U_UNBOUND"


def test_build_session_context_respects_explicit_channel_override(monkeypatch, tmp_path):
    """An explicit channel_id (Delivery hydration) still wins over the binding."""

    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        session = _seed_slack_dm_session(conn, tmp_path, dm_chat_id="D_REAL", user_id="U_OVERRIDE")

    context = internal_server._build_session_context(session["id"], channel_id="D_EXPLICIT")

    assert context.channel_id == "D_EXPLICIT"


def test_show_access_settings_read_returns_controller_snapshot(monkeypatch):
    from core import show_pages

    captured: list[str] = []

    class _Store:
        def get_access(self, page_id):
            captured.append(page_id)
            return show_pages.ShowAccess(
                page_id=page_id,
                access_mode="limited",
                share_id="stable-link",
                revision=7,
                normalized_emails=("alice@example.com", "bob@example.com"),
            )

        def close(self):
            captured.append("closed")

    monkeypatch.setattr(show_pages, "ShowPageStore", _Store)
    app = internal_server.create_app(_build_controller_double())

    async def _exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/internal/show-access/settings-read",
                json={"page_id": "ses-show-access"},
            )

    response = asyncio.run(_exercise())

    assert response.status_code == 200
    assert response.json() == {
        "show_access": {
            "page_id": "ses-show-access",
            "access_mode": "limited",
            "share_id": "stable-link",
            "revision": 7,
            "normalized_emails": ["alice@example.com", "bob@example.com"],
            "entries": [
                {
                    "kind": "email",
                    "value": "alice@example.com",
                    "organization_id": None,
                },
                {
                    "kind": "email",
                    "value": "bob@example.com",
                    "organization_id": None,
                },
            ],
        }
    }
    assert captured == ["ses-show-access", "closed"]


@pytest.mark.parametrize(
    ("path", "payload", "error"),
    [
        (
            "/internal/show-access/settings-read",
            {"page_id": "ses-show-access", "extra": True},
            "invalid_show_access_settings_request",
        ),
        (
            "/internal/show-access/apply",
            {
                "page_id": "ses-show-access",
                "expected_revision": True,
                "target_access_mode": "limited",
                "target_share_id": "stable-link",
                "target_emails": ["guest@example.com"],
            },
            "invalid_show_access_apply_request",
        ),
    ],
)
def test_show_access_internal_routes_reject_malformed_payloads(path, payload, error):
    app = internal_server.create_app(_build_controller_double())

    async def _exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(path, json=payload)

    response = asyncio.run(_exercise())

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": error}


@pytest.mark.parametrize("status", ["conflict", "share_id_taken"])
def test_show_access_apply_preserves_atomic_result_status(monkeypatch, status):
    from core import show_pages

    captured: dict = {}

    class _Store:
        def apply_access(self, page_id, **kwargs):
            captured.update(page_id=page_id, **kwargs)
            return show_pages.ShowAccessApplyResult(
                status=status,
                show_access=show_pages.ShowAccess(
                    page_id=page_id,
                    access_mode="public",
                    share_id="current-link",
                    revision=9,
                    normalized_emails=(),
                ),
            )

        def close(self):
            return None

    monkeypatch.setattr(show_pages, "ShowPageStore", _Store)
    app = internal_server.create_app(_build_controller_double())
    payload = {
        "page_id": "ses-show-access",
        "expected_revision": 8,
        "target_access_mode": "limited",
        "target_share_id": "candidate-link",
        "target_emails": ["guest@example.com"],
    }

    async def _exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/internal/show-access/apply", json=payload)

    response = asyncio.run(_exercise())

    assert response.status_code == 200
    assert response.json() == {
        "status": status,
        "show_access": {
            "page_id": "ses-show-access",
            "access_mode": "public",
            "share_id": "current-link",
            "revision": 9,
            "normalized_emails": [],
            "entries": [],
        },
    }
    assert captured == {
        "page_id": "ses-show-access",
        "expected_revision": 8,
        "target_access_mode": "limited",
        "target_share_id": "candidate-link",
        "target_emails": ["guest@example.com"],
    }


def test_show_access_apply_writes_and_reads_group_and_organization_entries(monkeypatch):
    from core import show_pages

    captured: dict = {}

    class _Store:
        def apply_access(self, page_id, **kwargs):
            captured.update(page_id=page_id, **kwargs)
            return show_pages.ShowAccessApplyResult(
                status="applied",
                show_access=show_pages.ShowAccess(
                    page_id=page_id,
                    access_mode="limited",
                    share_id="stable-link",
                    revision=2,
                    entries=(
                        show_pages.ShowAccessEntry("group", "group-7", "org-1"),
                        show_pages.ShowAccessEntry("organization", "org-1", "org-1"),
                    ),
                ),
            )

        def close(self):
            return None

    monkeypatch.setattr(show_pages, "ShowPageStore", _Store)
    app = internal_server.create_app(_build_controller_double())
    payload = {
        "page_id": "ses-show-access",
        "expected_revision": 1,
        "target_access_mode": "limited",
        "target_share_id": "stable-link",
        "target_entries": [
            {"kind": "group", "value": "group-7"},
            {"kind": "organization", "value": "org-1"},
        ],
    }

    async def _exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/internal/show-access/apply", json=payload)

    response = asyncio.run(_exercise())

    assert response.status_code == 200
    assert response.json() == {
        "status": "applied",
        "show_access": {
            "page_id": "ses-show-access",
            "access_mode": "limited",
            "share_id": "stable-link",
            "revision": 2,
            "normalized_emails": [],
            "entries": [
                {"kind": "group", "value": "group-7", "organization_id": "org-1"},
                {
                    "kind": "organization",
                    "value": "org-1",
                    "organization_id": "org-1",
                },
            ],
        },
    }
    assert captured["target_entries"] == payload["target_entries"]
    assert "target_emails" not in captured


def test_show_access_internal_identity_mismatch_fails_closed(monkeypatch):
    from core import show_pages

    class _Store:
        def get_access(self, _page_id):
            return show_pages.ShowAccess(
                page_id="ses-other",
                access_mode="limited",
                share_id="secret-link",
                revision=2,
                normalized_emails=("secret@example.com",),
            )

        def close(self):
            return None

    monkeypatch.setattr(show_pages, "ShowPageStore", _Store)
    app = internal_server.create_app(_build_controller_double())

    async def _exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/internal/show-access/settings-read",
                json={"page_id": "ses-show-access"},
            )

    response = asyncio.run(_exercise())

    assert response.status_code == 500
    assert response.json() == {
        "ok": False,
        "error": "show_access_internal_failure",
    }
    assert "secret@example.com" not in response.text


def test_show_access_apply_serializes_controller_writes(monkeypatch):
    from core import show_pages

    first_entered = threading.Event()
    release_first = threading.Event()
    calls: list[str] = []
    calls_lock = threading.Lock()

    class _Store:
        def apply_access(self, page_id, **_kwargs):
            with calls_lock:
                calls.append(page_id)
                call_number = len(calls)
            if call_number == 1:
                first_entered.set()
                assert release_first.wait(timeout=2)
            return show_pages.ShowAccessApplyResult(
                status="no_change",
                show_access=show_pages.ShowAccess(
                    page_id=page_id,
                    access_mode="private",
                    share_id="stable-link",
                    revision=0,
                    normalized_emails=(),
                ),
            )

        def close(self):
            return None

    monkeypatch.setattr(show_pages, "ShowPageStore", _Store)
    app = internal_server.create_app(_build_controller_double())
    payload = {
        "expected_revision": 0,
        "target_access_mode": "private",
        "target_share_id": "stable-link",
        "target_emails": [],
    }

    async def _exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = asyncio.create_task(
                client.post(
                    "/internal/show-access/apply",
                    json={"page_id": "ses-first", **payload},
                )
            )
            assert await asyncio.to_thread(first_entered.wait, 2)
            second = asyncio.create_task(
                client.post(
                    "/internal/show-access/apply",
                    json={"page_id": "ses-second", **payload},
                )
            )
            await asyncio.sleep(0.05)
            with calls_lock:
                assert calls == ["ses-first"]
            release_first.set()
            return await asyncio.gather(first, second)

    responses = asyncio.run(_exercise())

    assert [response.status_code for response in responses] == [200, 200]
    assert calls == ["ses-first", "ses-second"]
