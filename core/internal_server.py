"""Controller-side ASGI server bound to a Unix Domain Socket.

This is the C4 piece of Plan 2 from
``docs/plans/workbench-dispatch-architecture.md``: the controller process
exposes a minimal FastAPI app on
``~/.vibe_remote/state/dispatch.sock`` so cross-process callers (the
separate UI server subprocess, future ``vibe agent run --sync`` flows)
can submit turns to the controller-owned session turn manager.

Three properties matter:

1. **Same asyncio loop as the controller.** The server runs as a
   background ``asyncio.Task`` on the loop that ``Controller.run()``
   creates. IM adapters share that loop. No cross-loop futures, no
   second uvicorn worker, no thread bridge.
2. **Local-only.** Unix sockets are bind to a file path on the local
   filesystem; no TCP listen, so external network exposure is
   impossible.
3. **Restrictive permissions.** The socket file is created under a
   restrictive umask and chmod'd to ``0o600`` when the filesystem supports
   it — defense in depth against shared hosts.

The endpoint set is intentionally tiny; follow-ups can grow it without
changing the bind contract.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import json
import logging
import os
import socket
import stat
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional, TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.exc import IntegrityError

from config import paths
from core.services.dispatch import SOURCE_HUMAN, SOURCE_SCHEDULED
from modules.im.base import MessageContext
from storage.db import get_cached_sqlite_engine
from vibe.message_identity import HARNESS_TYPE, is_input_turn

if TYPE_CHECKING:  # pragma: no cover - typing only
    from core.controller import Controller

logger = logging.getLogger(__name__)
_SOCKET_MODE = 0o600
_SOCKET_UMASK_MODE = 0o700
_CHECK_POSIX_SOCKET_MODE = os.name != "nt"
_UNSUPPORTED_SOCKET_CHMOD_ERRNOS = frozenset(
    value
    for value in (
        errno.EINVAL,
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
    )
    if value is not None
)


def _create_controller_loop_server(config: Any) -> Any:
    """Create a uvicorn server without taking over process signal handlers."""

    import uvicorn

    class _ControllerLoopServer(uvicorn.Server):
        # Uvicorn >= 0.29 wraps serve() in capture_signals(); older supported
        # versions call install_signal_handlers() instead. The controller owns
        # this process and its event loop, so both hooks must remain inert.
        def capture_signals(self):
            return contextlib.nullcontext()

        def install_signal_handlers(self) -> None:
            return None

    return _ControllerLoopServer(config)


def default_socket_path() -> Path:
    """Where the internal server binds by default.

    By default this lives under ``~/.vibe_remote/state/`` for backward
    compatibility. Container runtimes can override it with
    ``VIBE_INTERNAL_DISPATCH_SOCKET`` when the persisted state mount does not
    support Unix-socket permission operations.
    """

    override = os.environ.get("VIBE_INTERNAL_DISPATCH_SOCKET")
    if override:
        return Path(override).expanduser()
    return paths.get_state_dir() / "dispatch.sock"


def create_app(
    controller: "Controller",
    *,
    memory_ui_secret: str | None = None,
) -> FastAPI:
    """Build the minimal FastAPI app the internal server exposes.

    Factored out so tests can mount the same routes against a fake
    controller without spinning up uvicorn.
    """
    from core.inbox_events import mark_controller_process

    mark_controller_process()
    if memory_ui_secret is None:
        from core.memory.ui_access import process_ui_read_secret

        memory_ui_secret = process_ui_read_secret()
    from core.memory.runtime import MemoryStoreUnavailableError

    app = FastAPI(
        title="avibe internal dispatch",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # In-flight ``dispatch_turn`` tasks per session, each a ``Turn`` holding the
    # task + the routing ``MessageContext`` the turn STARTED under. The cancel
    # endpoint looks the task up here so the UI can stop a runaway turn without
    # waiting for the agent to settle, and reuses the stored context so it
    # interrupts the backend the turn actually started on — even if the Chat
    # header changed the session's agent / model while the reply was streaming.
    # Tasks are registered by ``SessionTurnManager`` before they run and removed
    # in its ``finally`` so cancelled / completed sessions don't leak slots.
    # The turn owner (FSM) is created in Controller.__init__ so it exists for boot
    # stale-reset + OpenCode restore; reuse it here and bind the routing-context
    # builder now that the gate (which owns _build_session_context) is built. A fake
    # controller in tests may lack one — create it then. The registry bound below
    # is the SAME object the closures + ``controller.session_turn_gate`` use.
    from core.session_turns import SessionTurnManager

    manager = getattr(controller, "session_turns", None)
    if not isinstance(manager, SessionTurnManager):
        # Real controllers create it in __init__; a fake/Mock controller in tests
        # exposes a truthy stand-in, so gate on the type, not truthiness.
        manager = SessionTurnManager(controller)
        controller.session_turns = manager
    manager.bind_context(_build_session_context)

    # The turn registry (``session_id -> Turn``) is owned by the manager and tests
    # inspect it via ``app.state``. Flush intents live on each ``Turn`` (set by
    # ``manager.cancel`` / ``manager.send_now``), not in side sets here.
    in_flight = manager.in_flight
    app.state.in_flight_dispatches = in_flight

    def _publish_scheduled_queue_growth(session_id: str, state: str) -> None:
        if state != "queued":
            return
        try:
            from core.inbox_events import bus

            bus.publish("queue.updated", {"session_id": session_id})
        except Exception:
            logger.exception(
                "scheduled Delivery queue projection failed for Session=%s",
                session_id,
            )

    async def _submit_scheduled_turn(
        session_id: str,
        context: MessageContext,
        text: str,
        *,
        delivery_intent: str = "steer",
    ) -> Any:
        """Run a Harness input through the same durable owner as interactive Chat.

        The explicit source intent decides whether it queues, steers, or replaces.
        The submission remains a Delivery until exact native acceptance materializes
        the harness Message.
        """
        if not session_id:
            submission = await manager.submit(None, context, text, source=SOURCE_SCHEDULED)
            return submission.route

        from core.message_mirror import _scope_id_for_session
        from core.session_turns import (
            DeliveryRequest,
            SCHEDULED_PROVENANCE_KEY,
            TurnSubmissionResult,
            capture_scheduled_provenance,
        )
        from storage import message_deliveries, messages_service
        from storage.background import (
            agent_run_cancellation_won_in_connection,
            attach_agent_run_delivery_in_connection,
            cancel_queued_agent_run_delivery_in_connection,
            normalize_run_status,
            record_agent_run_delivery_outcome_in_connection,
            run_update_event_transaction,
        )

        submission_platform = str(getattr(context, "platform", None) or "avibe").strip()
        native_message_id = str(getattr(context, "message_id", None) or "").strip()
        submission_spec = dict(getattr(context, "platform_specific", None) or {})
        author_id = str(submission_spec.get("task_definition_id") or "").strip() or None
        author_name = str(submission_spec.get("task_trigger_kind") or "").strip() or None
        dedupe_key: str | None = None
        delivery_id: str | None = None
        delivery_request: DeliveryRequest | None = None
        scope_id: str | None = None
        delivery_owner_transferred = False
        target_was_busy = False
        effective_delivery_intent = delivery_intent
        execution_id = str(
            (context.platform_specific or {}).get("task_execution_id") or ""
        ).strip()
        with run_update_event_transaction(get_cached_sqlite_engine()) as conn:
            from sqlalchemy import select
            from storage.agent_session_rows import reserve_write_lock
            from storage.models import agent_runs, agent_sessions

            reserve_write_lock(conn)
            scope_id = _scope_id_for_session(conn, session_id)
            dedupe_key = message_deliveries.native_dedupe_key(
                submission_platform,
                native_message_id,
                scope_id=scope_id,
            )
            existing = message_deliveries.get_delivery_by_native_identity(
                conn,
                platform=submission_platform,
                native_message_id=native_message_id,
                scope_id=scope_id,
                session_id=session_id,
                normalize_legacy=True,
            )
            legacy_accepted = bool(
                native_message_id
                and messages_service.native_message_exists(
                    conn,
                    platform=submission_platform,
                    scope_id=scope_id,
                    native_message_id=native_message_id,
                )
            )
            target_was_busy = message_deliveries.active_turn(conn, session_id) is not None
            if existing is not None and existing["session_id"] != session_id:
                return "duplicate"
            if existing is not None and existing["state"] != "reserved":
                if execution_id:
                    run_owner = conn.execute(
                        select(
                            agent_runs.c.status,
                            agent_runs.c.session_id,
                            agent_runs.c.delivery_id,
                        )
                        .where(agent_runs.c.id == execution_id)
                        .limit(1)
                    ).mappings().first()
                    if (
                        run_owner is not None
                        and normalize_run_status(run_owner["status"])
                        in {"queued", "running"}
                        and run_owner["session_id"] == session_id
                        and run_owner["delivery_id"] == existing["id"]
                    ):
                        existing_state = str(existing["state"])
                        if existing_state == "retired":
                            cancel_queued_agent_run_delivery_in_connection(
                                conn,
                                execution_id,
                                session_id=session_id,
                                delivery_id=str(existing["id"]),
                            )
                            return TurnSubmissionResult(
                                route="enqueued",
                                queue_persisted=False,
                                target_was_busy=target_was_busy,
                                delivery_status="canceled",
                            )
                        return TurnSubmissionResult(
                            route=(
                                "enqueued"
                                if existing_state
                                in {
                                    "queued",
                                    "interrupt_waiting",
                                    "waiting_terminal",
                                    "reconciling_steer",
                                }
                                else "ran"
                            ),
                            queue_persisted=True,
                            target_was_busy=target_was_busy,
                            delivery_status=existing_state,
                            delivery_owner_transferred=True,
                        )
                return "duplicate"
            if legacy_accepted:
                return "duplicate"
            status = conn.execute(
                select(agent_sessions.c.status).where(agent_sessions.c.id == session_id)
            ).scalar_one_or_none()
            if status != "active":
                raise ValueError("Session is archived")
            if existing is not None:
                delivery_id = str(existing["id"])
                delivery_request = manager._request_from_delivery(existing)
                scope_id = delivery_request.scope_id
                from core.message_priority import delivery_intent_for_priority

                effective_delivery_intent = delivery_intent_for_priority(
                    delivery_request.priority
                )
                provenance = (delivery_request.metadata or {}).get(
                    SCHEDULED_PROVENANCE_KEY
                )
                persisted_spec = (
                    provenance.get("platform_specific")
                    if isinstance(provenance, dict)
                    else None
                )
                if isinstance(persisted_spec, dict):
                    execution_id = str(
                        persisted_spec.get("task_execution_id") or execution_id
                    ).strip()
            else:
                from core.message_priority import priority_for_delivery_intent

                delivery_id = message_deliveries.new_delivery_id()
                admitted_state = "reserved"
                priority = priority_for_delivery_intent(delivery_intent)
                message_deliveries.insert_delivery(
                    conn,
                    delivery_id=delivery_id,
                    session_id=session_id,
                    priority=priority,
                    state=admitted_state,
                    snapshot=message_deliveries.message_snapshot(
                        scope_id=scope_id,
                        session_id=session_id,
                        platform=submission_platform,
                        author="harness",
                        author_id=author_id,
                        author_name=author_name,
                        source="harness",
                        message_type="harness",
                        text=text,
                        metadata={
                            SCHEDULED_PROVENANCE_KEY: capture_scheduled_provenance(
                                context
                            )
                        },
                        native_message_id=native_message_id or None,
                    ),
                    dispatch_text=text,
                    dedupe_key=dedupe_key,
                    history_event={
                        "kind": "admission",
                        "priority": priority,
                        "state": admitted_state,
                    },
                )
            if execution_id:
                if not attach_agent_run_delivery_in_connection(
                    conn,
                    execution_id,
                    session_id=session_id,
                    delivery_id=delivery_id,
                    delivery_outcome={
                        "intent": effective_delivery_intent,
                        "status": "admitted",
                        "target_was_busy": target_was_busy,
                    },
                ):
                    if agent_run_cancellation_won_in_connection(conn, execution_id):
                        message_deliveries.retire_not_written(
                            conn,
                            session_id,
                            delivery_id,
                            reason="agent_run_canceled",
                        )
                        return TurnSubmissionResult(
                            route="enqueued",
                            queue_persisted=False,
                            delivery_status="canceled",
                        )
                    raise RuntimeError("Agent Run Delivery binding was refused")
                delivery_owner_transferred = True

        if delivery_request is None:
            from core.message_priority import priority_for_delivery_intent

            delivery_request = DeliveryRequest(
                session_id=session_id,
                priority=priority_for_delivery_intent(delivery_intent),
                content=text,
                delivery_id=delivery_id,
                scope_id=scope_id,
                platform=submission_platform,
                source="harness",
                author="harness",
                author_id=author_id,
                author_name=author_name,
                message_type="harness",
                display_text=text,
                metadata={
                    SCHEDULED_PROVENANCE_KEY: capture_scheduled_provenance(context)
                },
                native_message_id=native_message_id or None,
            )

        if context.platform_specific is None:
            context.platform_specific = {}
        context.platform_specific.update(
            {
                "delivery_id": delivery_id,
                "native_message_id": delivery_request.native_message_id,
                "scope_id": scope_id,
                "display_text": delivery_request.display_text,
                "message_metadata": dict(delivery_request.metadata or {}),
                "author_id": delivery_request.author_id,
                "author_name": delivery_request.author_name,
            }
        )
        try:
            result = await manager.deliver(
                delivery_request,
                context=context,
            )
        except Exception:
            if not delivery_owner_transferred:
                raise
            logger.exception(
                "Delivery admission deferred to recovery after Agent Run ownership "
                "transfer: run=%s delivery=%s",
                execution_id,
                delivery_id,
            )
            delivery_status = "reserved"
            try:
                with run_update_event_transaction(get_cached_sqlite_engine()) as conn:
                    persisted = message_deliveries.get_delivery(conn, delivery_id)
                    delivery_status = str(
                        (persisted or {}).get("state") or delivery_status
                    )
                    recorded = record_agent_run_delivery_outcome_in_connection(
                        conn,
                        execution_id,
                        {
                            "intent": effective_delivery_intent,
                            "status": delivery_status,
                            "target_was_busy": target_was_busy,
                        },
                    )
                if not recorded:
                    logger.warning(
                        "Agent Run deferred Delivery outcome lost its exact CAS: "
                        "run=%s delivery=%s",
                        execution_id,
                        delivery_id,
                    )
            except Exception:
                logger.exception(
                    "Agent Run deferred outcome projection failed; Delivery "
                    "ownership remains authoritative: run=%s delivery=%s",
                    execution_id,
                    delivery_id,
                )
            _publish_scheduled_queue_growth(session_id, delivery_status)
            return TurnSubmissionResult(
                route="enqueued",
                queue_persisted=True,
                target_was_busy=target_was_busy,
                delivery_status=delivery_status,
                delivery_owner_transferred=True,
            )
        route = "enqueued" if result.state in {
            "queued",
            "interrupt_waiting",
            "waiting_terminal",
            "reconciling_steer",
        } else "ran"
        submission = TurnSubmissionResult(
            route=route,
            queue_persisted=True,
            target_was_busy=target_was_busy,
            delivery_status=(
                result.state if effective_delivery_intent == "send_now" else None
            ),
            delivery_owner_transferred=delivery_owner_transferred,
        )
        _publish_scheduled_queue_growth(session_id, str(result.state))
        if effective_delivery_intent == "send_now":
            try:
                with run_update_event_transaction(get_cached_sqlite_engine()) as conn:
                    recorded = record_agent_run_delivery_outcome_in_connection(
                        conn,
                        execution_id,
                        {
                            "intent": "send_now",
                            "status": result.state,
                            "target_was_busy": submission.target_was_busy,
                        },
                    )
                if not recorded:
                    logger.warning(
                        "send-now Agent Run outcome lost its exact CAS after Delivery "
                        "ownership transfer: run=%s delivery=%s",
                        execution_id,
                        delivery_id,
                    )
            except Exception:
                logger.exception(
                    "send-now Agent Run outcome persistence deferred after Delivery "
                    "ownership transfer: run=%s delivery=%s",
                    execution_id,
                    delivery_id,
                )
            return submission
        return submission if delivery_owner_transferred else route

    @app.get("/internal/health")
    async def _health() -> dict[str, Any]:
        return {"ok": True, "service": "vibe-remote-internal", "version": 1}

    @app.get("/internal/turn-state/{session_id}")
    async def _turn_state(session_id: str) -> Any:
        """HTTP adapter: whether a turn is running, delegated to the turn owner
        (FSM, Phase 1b). A reconnected Chat page asks this to restore working/Stop."""
        return manager.turn_state(session_id)

    @app.get("/internal/running-agents")
    async def _running_agents() -> Any:
        """Read-only snapshot of currently-running agent instances across all
        backends. Lives here because every liveness source is controller
        in-memory state the UI process cannot see; the web ``/api/running-agents``
        route proxies this. Never mutates sessions/transports/eviction state.

        Offloaded to a worker thread: the snapshot does a synchronous SQLite read
        (and, when live orphan candidates survive, ``ps`` probes), which must not
        block the controller's event loop that also serves IM/dispatch/SSE. The
        aggregator tolerates concurrent registry mutation (``_safe_items``)."""
        from core.services.running_agents import snapshot_running_agents

        return await asyncio.to_thread(snapshot_running_agents, controller)

    @app.post("/internal/running-agents/end")
    async def _running_agents_end(request: Request) -> Any:
        """Terminate one running agent's live runtime (Stop turn / disconnect /
        kill orphan process), dispatched by backend+state. Runs ON the loop (it
        awaits backend interrupts and mutates loop-owned registries — must NOT be
        offloaded). Deliberately has no self-kill guard."""
        from core.services.running_agents import end_running_agent

        payload = await _safe_json(request)
        if not isinstance(payload, dict):
            return JSONResponse(status_code=400, content={"ok": False, "error": "invalid_payload"})
        raw_pid = payload.get("pid")
        try:
            pid = int(raw_pid) if raw_pid is not None else None
        except (TypeError, ValueError):
            pid = None
        result = await end_running_agent(
            controller,
            backend=(str(payload.get("backend")).strip() or None) if payload.get("backend") else None,
            state=(str(payload.get("state")).strip() or None) if payload.get("state") else None,
            session_id=payload.get("session_id") or None,
            composite_key=payload.get("composite_key") or None,
            base_session_id=payload.get("base_session_id") or None,
            pid=pid,
        )
        if not result.get("ok"):
            return JSONResponse(status_code=409, content=result)
        return result

    @app.post("/internal/dispatch_async")
    async def _dispatch_async(request: Request) -> Any:
        """Admit one already-persisted Delivery on the controller loop."""

        from storage import message_deliveries
        from storage import workbench_sessions_service

        payload = await _safe_json(request)
        session_id = str(payload.get("session_id") or "").strip()
        delivery_id = str(payload.get("user_message_id") or "").strip()
        if not delivery_id:
            from core.workbench_media import file_attachments_from_specs

            raw_text = payload.get("text")
            if not (
                isinstance(raw_text, str) and raw_text.strip()
            ) and not file_attachments_from_specs(payload.get("files")):
                return JSONResponse(
                    status_code=400,
                    content={"ok": False, "error": "text or files is required"},
                )
        if not session_id or not delivery_id:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "session_id and user_message_id are required"},
            )
        with get_cached_sqlite_engine().begin() as conn:
            from storage.agent_session_rows import reserve_write_lock

            reserve_write_lock(conn)
            delivery = message_deliveries.get_delivery(conn, delivery_id)
            archived = workbench_sessions_service.is_session_archived(conn, session_id)
            if (
                archived
                and delivery is not None
                and delivery["session_id"] == session_id
                and delivery["state"] == "reserved"
            ):
                message_deliveries.retire_not_written(
                    conn,
                    session_id,
                    delivery_id,
                    reason="session_archived",
                )
                delivery = message_deliveries.get_delivery(conn, delivery_id)
            delivery_payload = (
                message_deliveries.delivery_payload(delivery)
                if delivery is not None and delivery["session_id"] == session_id
                else None
            )
            if (
                delivery is not None
                and delivery_payload is not None
                and message_deliveries.delivery_has_remote_resource_context(delivery)
            ):
                message_deliveries.retire_not_written(
                    conn,
                    session_id,
                    delivery_id,
                    reason="remote_execution_disabled",
                )
                return JSONResponse(
                    status_code=403,
                    content={
                        "ok": False,
                        "code": "remote_execution_disabled",
                        "session_id": session_id,
                        "delivery_id": delivery_id,
                    },
                )
            attachment_specs: list[dict[str, Any]] = []
            if delivery_payload is not None:
                from core.workbench_media import resolve_attachment_specs

                attachment_specs = resolve_attachment_specs(
                    conn,
                    session_id=session_id,
                    attachments=(delivery_payload.get("content") or {}).get(
                        "attachments"
                    )
                    or [],
                )
        if archived or delivery is None or delivery["session_id"] != session_id:
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "code": "session_archived" if archived else "delivery_reservation_lost",
                    "session_id": session_id,
                    "delivery_id": delivery_id,
                },
            )
        if delivery["state"] != "reserved":
            return JSONResponse(
                status_code=202,
                content={
                    "ok": True,
                    "duplicate": True,
                    "session_id": session_id,
                    "delivery_id": delivery_id,
                    "message_id": delivery.get("message_id"),
                    "delivery_state": delivery["state"],
                    "queued": delivery["state"] == "queued",
                },
            )
        # The HTTP request only wakes the durable owner. Prompt, provenance, and
        # attachments always come from the reservation, never from a stale caller.
        dispatch_payload = {
            **payload,
            "text": str(delivery.get("dispatch_text") or ""),
            "files": attachment_specs,
            "platform": delivery_payload.get("platform"),
            "user_id": delivery_payload.get("author_id"),
            "message_id": delivery_payload.get("native_message_id") or delivery_id,
            "thread_id": delivery_payload.get("parent_native_message_id"),
            "scope_id": delivery_payload.get("scope_id"),
            "display_text": delivery_payload.get("text"),
            "content": dict(delivery_payload.get("content") or {}),
            "metadata": dict(delivery_payload.get("metadata") or {}),
            "author_id": delivery_payload.get("author_id"),
            "author_name": delivery_payload.get("author_name"),
        }
        try:
            text, context = await _build_dispatch_payload(dispatch_payload)
        except ValueError as err:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(err)})
        if context.platform_specific is None:
            context.platform_specific = {}
        context.platform_specific.update(
            {
                "delivery_id": delivery_id,
                "scope_id": dispatch_payload.get("scope_id"),
                "display_text": dispatch_payload.get("display_text"),
                "message_content": dispatch_payload.get("content"),
                "message_metadata": dispatch_payload.get("metadata") or {},
                "author_id": dispatch_payload.get("author_id"),
                "author_name": dispatch_payload.get("author_name"),
            }
        )
        from core.message_priority import delivery_intent_for_priority

        delivery_intent = delivery_intent_for_priority(str(delivery["priority"]))
        submission = await manager.submit(
            session_id,
            context,
            text,
            delivery_intent=delivery_intent,
        )
        with get_cached_sqlite_engine().connect() as conn:
            settled = message_deliveries.get_delivery(conn, delivery_id)
        return JSONResponse(
            status_code=202,
            content={
                "ok": True,
                "session_id": session_id,
                "delivery_id": delivery_id,
                "message_id": (settled or {}).get("message_id"),
                "delivery_state": (settled or {}).get("state"),
                "queued": submission.route == "enqueued",
            },
        )

    @app.post("/internal/reconcile-platforms")
    async def _reconcile_platforms() -> Any:
        """Hot-apply the persisted platform config on the controller loop."""
        try:
            from config.v2_compat import to_app_config
            from config.v2_config import V2Config

            result = await controller.reconcile_platforms(to_app_config(V2Config.load()))
            return JSONResponse(status_code=200, content=result)
        except Exception as exc:
            logger.exception("internal platform reconcile failed")
            return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})

    @app.post("/internal/reconcile-agent-backends")
    async def _reconcile_agent_backends(request: Request) -> Any:
        """Hot-apply persisted Agent backend config on the controller loop."""
        payload = await _safe_json(request)
        backends = payload.get("backends")
        if not isinstance(backends, list) or not all(isinstance(item, str) for item in backends):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "backends must be a list of strings"},
            )
        try:
            result = await controller.reconcile_agent_backends(backends)
            return JSONResponse(status_code=200, content=result)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
        except Exception as exc:
            logger.exception("internal Agent backend reconcile failed")
            return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})

    def _memory_runtime():
        return getattr(controller, "memory_runtime", None)

    @app.post("/internal/reconcile-memory")
    async def _reconcile_memory() -> Any:
        """Hot-apply persisted Memory configuration on the controller loop."""

        try:
            from config.v2_config import V2Config

            config = await asyncio.to_thread(V2Config.load)
            result = await controller.reconcile_memory(config.memory)
            return JSONResponse(status_code=200, content=result)
        except Exception:
            logger.warning("internal memory reconcile failed")
            return JSONResponse(status_code=503, content={"ok": False, "error": "memory_runtime_install_failed"})

    @app.post("/internal/memory/install-runtime")
    async def _memory_install_runtime() -> Any:
        """Install or repair the managed runtime on the controller lifecycle."""

        runtime = _memory_runtime()
        if runtime is None:
            return JSONResponse(status_code=503, content={"ok": False, "reason": "memory_runtime_missing"})
        try:
            result = await runtime.install_artifact()
            return JSONResponse(status_code=200, content=result)
        except Exception:
            logger.warning("internal memory runtime install failed")
            return JSONResponse(status_code=503, content={"ok": False, "reason": "memory_runtime_install_failed"})

    def _memory_cli_principal(request: Request) -> str | None:
        from core.memory.http_headers import CALLER_SESSION_HEADER

        session_id = str(request.headers.get(CALLER_SESSION_HEADER) or "").strip()
        if not session_id:
            return None
        resolve = getattr(controller, "memory_principal_for_cli_session", None)
        principal_id = resolve(session_id) if callable(resolve) else None
        from core.memory.store import is_principal_id

        return principal_id if is_principal_id(principal_id) else None

    def _verified_memory_ui_user_key(request: Request) -> str | None:
        from core.memory.http_headers import (
            CALLER_SESSION_HEADER,
            MEMORY_USER_KEY_HEADER,
        )

        session_id = str(request.headers.get(CALLER_SESSION_HEADER) or "").strip()
        user_key = str(request.headers.get(MEMORY_USER_KEY_HEADER) or "").strip()
        if session_id or user_key != "avibe:local":
            return None
        from core.memory.ui_access import MEMORY_UI_PROOF_HEADER, verify_ui_read_proof

        proof = str(request.headers.get(MEMORY_UI_PROOF_HEADER) or "").strip()
        if memory_ui_secret is None or not verify_ui_read_proof(
            memory_ui_secret,
            proof,
            method=request.method,
            path=request.url.path,
            user_key=user_key,
        ):
            return None
        return user_key

    def _memory_read_principal(request: Request) -> str | None:
        from core.memory.http_headers import MEMORY_USER_KEY_HEADER

        if str(request.headers.get(MEMORY_USER_KEY_HEADER) or "").strip():
            user_key = _verified_memory_ui_user_key(request)
            if user_key is None:
                return None
            runtime = _memory_runtime()
            try:
                return runtime.principal_for_user_key(user_key) if runtime is not None else None
            except MemoryStoreUnavailableError:
                raise
            except Exception as exc:
                raise MemoryStoreUnavailableError("Memory store is unavailable") from exc
        return _memory_cli_principal(request)

    @app.get("/internal/memory/status")
    async def _memory_status() -> Any:
        runtime = _memory_runtime()
        if runtime is None:
            return JSONResponse(status_code=503, content={"error": "memory_runtime_missing"})
        try:
            return await runtime.status_payload()
        except Exception:
            logger.warning("internal memory status failed")
            return JSONResponse(status_code=503, content={"error": "memory_store_unavailable"})

    @app.get("/internal/memory/failures")
    async def _memory_failures() -> Any:
        runtime = _memory_runtime()
        if runtime is None:
            return JSONResponse(status_code=503, content={"error": "memory_runtime_missing"})
        try:
            return await runtime.failure_log_payload()
        except Exception:
            logger.warning("internal memory failure log failed")
            return JSONResponse(status_code=503, content={"error": "memory_store_unavailable"})

    @app.get("/internal/memory/profile")
    async def _memory_profile(request: Request) -> Any:
        try:
            principal_id = _memory_read_principal(request)
        except MemoryStoreUnavailableError:
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": "memory_store_unavailable"},
            )
        if principal_id is None:
            return JSONResponse(status_code=403, content={"status": "failed", "error": "memory_access_denied"})
        runtime = _memory_runtime()
        if runtime is None:
            return JSONResponse(status_code=503, content={"status": "failed", "error": "memory_runtime_missing"})
        try:
            return await runtime.profile_payload(principal_id)
        except Exception:
            logger.warning("internal memory profile failed")
            return JSONResponse(status_code=503, content={"status": "failed", "error": "memory_processing_failed"})

    @app.post("/internal/memory/search")
    async def _memory_search(request: Request) -> Any:
        try:
            principal_id = _memory_read_principal(request)
        except MemoryStoreUnavailableError:
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": "memory_store_unavailable"},
            )
        if principal_id is None:
            return JSONResponse(status_code=403, content={"status": "failed", "error": "memory_access_denied"})
        runtime = _memory_runtime()
        if runtime is None:
            return JSONResponse(status_code=503, content={"status": "failed", "error": "memory_runtime_missing"})
        payload = await _safe_json(request)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"query", "limit"}
            or not isinstance(payload.get("query"), str)
        ):
            return JSONResponse(status_code=400, content={"status": "failed", "error": "memory_invalid_input"})
        limit = payload.get("limit")
        if not isinstance(limit, int) or isinstance(limit, bool):
            return JSONResponse(status_code=400, content={"status": "failed", "error": "memory_invalid_input"})
        try:
            return await runtime.search_payload(payload["query"], limit, principal_id)
        except Exception:
            logger.warning("internal memory search failed")
            return JSONResponse(status_code=503, content={"status": "failed", "error": "memory_processing_failed"})

    @app.post("/internal/memory/remember")
    async def _memory_remember(request: Request) -> Any:
        principal_id = _memory_cli_principal(request)
        if principal_id is None:
            return JSONResponse(status_code=403, content={"status": "failed", "error": "memory_access_denied"})
        runtime = _memory_runtime()
        module = getattr(runtime, "module", None) if runtime is not None else None
        if module is None:
            return JSONResponse(status_code=503, content={"status": "failed", "error": "memory_runtime_missing"})
        payload = await _safe_json(request)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"text"}
            or not isinstance(payload.get("text"), str)
            or not payload["text"].strip()
            or len(payload["text"]) > 4_000
        ):
            return JSONResponse(status_code=400, content={"status": "failed", "error": "memory_invalid_input"})

        from core.memory import CaptureRequest
        from core.memory.http_headers import CALLER_SESSION_HEADER

        session_id = str(request.headers.get(CALLER_SESSION_HEADER) or "").strip()
        text = payload["text"]
        source_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

        try:
            receipt = await module.capture(
                CaptureRequest(
                    source_message_id=f"agent:{principal_id}:{session_id}:{source_digest}",
                    session_id=session_id,
                    principal_id=principal_id,
                    provenance="agent",
                    text=text,
                    occurred_at_ms=int(time.time() * 1000),
                )
            )
        except Exception:
            logger.warning("internal memory remember failed")
            return JSONResponse(status_code=503, content={"status": "failed", "error": "memory_store_unavailable"})
        response: dict[str, Any] = {"status": receipt.status}
        reason = getattr(receipt, "reason", None)
        error = getattr(receipt, "error", None)
        if reason is not None:
            response["reason"] = reason
        if error is not None:
            response["error"] = error
        return response

    @app.post("/internal/memory/clear")
    async def _memory_clear(request: Request) -> Any:
        if _verified_memory_ui_user_key(request) is None:
            return JSONResponse(status_code=403, content={"status": "failed", "error": "memory_access_denied"})
        runtime = _memory_runtime()
        if runtime is None:
            return JSONResponse(status_code=503, content={"status": "failed", "error": "memory_runtime_missing"})
        payload = await _safe_json(request)
        if payload != {"confirm": True}:
            return JSONResponse(status_code=400, content={"status": "failed", "error": "memory_invalid_input"})
        try:
            return await runtime.clear()
        except MemoryStoreUnavailableError:
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": "memory_store_unavailable"},
            )
        except Exception:
            logger.warning("internal memory clear failed")
            return JSONResponse(status_code=503, content={"status": "failed", "error": "memory_clear_failed"})

    @app.post("/internal/model-hub")
    async def _model_hub(request: Request) -> Any:
        """Dispatch UI operations to the controller-owned Model Hub aggregate."""

        from config.v2_config import is_model_hub_enabled
        from core.handlers.model_hub import ModelHubError
        from core.handlers.model_hub.rpc import dispatch_model_hub_rpc

        if not is_model_hub_enabled():
            return JSONResponse(
                status_code=404,
                content={"ok": False, "contract_version": 1, "error": "feature_disabled"},
            )
        body = await _safe_json(request)
        operation = body.get("operation") if isinstance(body, dict) else None
        payload = body.get("payload") if isinstance(body, dict) else None
        if not isinstance(operation, str) or not isinstance(payload, dict):
            return JSONResponse(status_code=400, content={"ok": False, "error": "discovery_failed"})
        service = getattr(controller, "model_hub_service", None)
        if service is None:
            return JSONResponse(status_code=503, content={"ok": False, "error": "engine_down"})
        try:
            result = await dispatch_model_hub_rpc(service, operation, payload)
        except ModelHubError as exc:
            response = {"ok": False, "error": exc.code}
            if exc.detail:
                response["detail"] = exc.detail
            response.update(exc.data)
            return JSONResponse(status_code=exc.status, content=response)
        return {"ok": True, "result": result}

    @app.get("/internal/events")
    async def _events() -> Any:
        """Long-lived SSE feed of Controller-side inbox events.

        The UI server opens this once on startup and re-broadcasts each event
        to browsers via its own SSEBroker, so realtime inbox updates (a new
        agent ``result`` bumping a session to the top) work across the
        process boundary.
        """
        from core.inbox_events import bus

        sub_id, queue = bus.subscribe()

        async def _stream():
            try:
                # A REAL ``connected`` event (not a ``:`` comment, which the
                # internal_client parser swallows) so it flows bridge → broker →
                # browser. The UI sidebar refetches on this, which reconciles
                # agent-status dots after a CONTROLLER restart while the UI server
                # + browser SSE stay up: only this bridge reconnects, so the
                # browser's own ``connected`` never fires and the crash-recovery
                # ``running → idle`` reset (broadcast to no subscriber) would
                # otherwise be invisible until a manual reload (Codex P2).
                yield _sse_event("connected", {})
                while True:
                    event_type, data = await queue.get()
                    yield _sse_event(event_type, data)
            finally:
                bus.unsubscribe(sub_id)

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/internal/events")
    async def _publish_event(request: Request) -> Any:
        """Publish an allowlisted notification into the Controller event bus.

        Local child processes cannot share ``core.inbox_events.bus`` directly,
        but they can reach this permission-restricted Unix socket and reuse the
        same Controller -> UI-server -> browser SSE path.
        """
        from core.inbox_events import (
            DEFINITIONS_UPDATED_EVENT,
            QUEUE_UPDATED_EVENT,
            RUNS_UPDATED_EVENT,
            VAULTS_UPDATED_EVENT,
            bus,
        )

        payload = await _safe_json(request)
        if not isinstance(payload, dict):
            return JSONResponse(status_code=400, content={"ok": False, "error": "invalid_payload"})
        event_type = str(payload.get("type") or "").strip()
        data = payload.get("data")
        if event_type not in {
            DEFINITIONS_UPDATED_EVENT,
            QUEUE_UPDATED_EVENT,
            RUNS_UPDATED_EVENT,
            VAULTS_UPDATED_EVENT,
        }:
            return JSONResponse(status_code=400, content={"ok": False, "error": "unsupported_event_type"})
        if not isinstance(data, dict):
            return JSONResponse(status_code=400, content={"ok": False, "error": "invalid_event_data"})
        bus.publish(event_type, data)
        return {"ok": True}

    @app.post("/internal/vault/request-created")
    async def _vault_request_created(request: Request) -> Any:
        """Notify the originating IM session that a Vault request needs web review."""

        payload = await _safe_json(request)
        request_payload = payload.get("request")
        if not isinstance(request_payload, dict):
            return JSONResponse(status_code=400, content={"ok": False, "error": "invalid_request"})
        from core.vault_request_notifications import notify_vault_request_created

        async def _runner() -> None:
            result = await notify_vault_request_created(controller, request_payload)
            if result.get("ok") is False:
                logger.debug("vault request notification failed: %s", result)

        asyncio.create_task(_runner(), name="vault-request-notification")
        return {"ok": True, "queued": True}

    @app.post("/internal/cancel/{session_id}")
    async def _cancel(session_id: str) -> Any:
        """HTTP adapter: delegate Stop to the turn owner (FSM, Phase 1b) and map its
        result ``code`` to a status — ``not_in_flight`` -> 404, ``stop_failed`` ->
        409. ``session_id`` is the dispatch key the turn registered under, so the UI
        Stop button works with just the URL it already has."""
        result = await manager.cancel(session_id)
        code = result.get("code")
        if code == "not_in_flight":
            return JSONResponse(status_code=404, content=result)
        if code in {"stop_failed", "stop_unknown"}:
            return JSONResponse(status_code=409, content=result)
        return result

    @app.post("/internal/send-now/{session_id}")
    async def _send_now(
        session_id: str,
        expected_delivery_id: str | None = None,
    ) -> Any:
        """HTTP adapter: delegate "立即发送" (run the send-while-busy queue now) to
        the turn owner (FSM, Phase 1b); typed failures remain HTTP failures."""
        result = await manager.send_now(
            session_id,
            expected_delivery_id=expected_delivery_id,
        )
        code = result.get("code")
        if code in {"stop_failed", "stale_head", "ordering_fence"}:
            return JSONResponse(status_code=409, content=result)
        if code == "flush_failed":
            return JSONResponse(status_code=503, content=result)
        return result

    # Expose the per-session turn gate to in-process callers (the scheduler)
    # WITHOUT going through the HTTP surface: ``ScheduledTaskService`` runs on the
    # same loop and routes persisted Session inputs through ``submit_scheduled``
    # so they share the Chat path's durable priority and lifecycle authority.
    # ``in_flight`` is the SAME dict object as ``app.state.in_flight_dispatches``
    # (the cancel endpoint, turn-state, and the tests all read it), so a scheduled
    # run registered by ``_run_turn`` is Stoppable through ``/internal/cancel``.
    controller.session_turn_gate = SimpleNamespace(
        submit_scheduled=_submit_scheduled_turn,
        in_flight=in_flight,
    )

    return app


async def serve(controller: "Controller", *, socket_path: Optional[Path] = None) -> None:
    """Run the internal server forever on the current event loop.

    Returns when the underlying uvicorn server exits (typically when the
    controller's loop is shut down). Each call binds a fresh socket
    file; pre-existing files at ``socket_path`` are removed first so
    restarts don't fail with "address already in use".

    Permissions: we tighten ``os.umask`` to ``0o077`` *before* uvicorn
    binds the socket so the file is created with mode ``0o700`` and is
    never readable / connectable by other local users — even briefly.
    A best-effort post-bind ``os.chmod`` then forces the final mode in
    case the platform's umask handling differs (some BSDs ignore umask
    for AF_UNIX bind). Without the umask wrap there is a TOCTOU window
    where the socket would be world-accessible between bind and chmod.
    """

    import uvicorn

    app = create_app(controller)
    recovery_complete = getattr(controller, "_delivery_recovery_complete", None)
    if recovery_complete is not None:
        await recovery_complete.wait()
    config = uvicorn.Config(
        app,
        log_config=None,
        access_log=False,
        loop="asyncio",
        lifespan="off",
    )
    server = _create_controller_loop_server(config)

    listener, target = _bind_socket(socket_path)
    _write_internal_server_status("ready")
    try:
        await server.serve(sockets=[listener])
    finally:
        try:
            listener.close()
        except OSError:
            pass
        _remove_owned_socket(target)


def _bind_socket(socket_path: Optional[Path] = None) -> tuple[socket.socket, Path]:
    """Pre-bind the Unix socket before handing it to uvicorn.

    Uvicorn binds ``uds=...`` itself and then chmods the path. Docker Desktop
    bind mounts can support AF_UNIX sockets but reject chmod on those socket
    pathnames with ``EINVAL``. Binding here and passing the open socket avoids
    uvicorn's path chmod while keeping the endpoint local-only.
    """

    target = (socket_path or default_socket_path()).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    _remove_stale_owned_socket(target)

    previous_umask = os.umask(0o077)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(target))
        listener.listen(2048)
        listener.setblocking(False)
        try:
            os.chmod(target, _SOCKET_MODE)
        except OSError as error:
            if error.errno not in _UNSUPPORTED_SOCKET_CHMOD_ERRNOS:
                raise
            logger.debug("internal dispatch socket chmod is unsupported for %s", target)
            _verify_owned_socket(target, allow_umask_mode=True)
        else:
            _verify_owned_socket(target)
        return listener, target
    except Exception:
        listener.close()
        _remove_socket_after_bind_failure(target)
        raise
    finally:
        os.umask(previous_umask)


def _remove_stale_owned_socket(target: Path) -> None:
    try:
        info = target.lstat()
    except FileNotFoundError:
        return
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise OSError("internal dispatch socket owner mismatch")
    # lstat + unlink removes the directory entry itself, including a symlink;
    # it never follows or mutates the path the stale entry may point at.
    target.unlink()


def _remove_owned_socket(target: Path) -> None:
    try:
        _verify_owned_socket(target, allow_umask_mode=True)
        target.unlink()
    except FileNotFoundError:
        return
    except OSError:
        logger.debug("could not unlink internal dispatch socket %s", target, exc_info=True)


def _remove_socket_after_bind_failure(target: Path) -> None:
    """Remove only a socket still owned by this user after a failed hardening step."""

    try:
        info = target.lstat()
        if stat.S_ISSOCK(info.st_mode) and (not hasattr(os, "getuid") or info.st_uid == os.getuid()):
            target.unlink()
    except FileNotFoundError:
        return
    except OSError:
        logger.debug("could not remove failed internal dispatch socket %s", target, exc_info=True)


def _verify_owned_socket(target: Path, *, allow_umask_mode: bool = False) -> None:
    info = target.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISSOCK(info.st_mode):
        raise OSError("internal dispatch socket is unsafe")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise OSError("internal dispatch socket owner mismatch")
    if _CHECK_POSIX_SOCKET_MODE:
        allowed_modes = {_SOCKET_MODE, _SOCKET_UMASK_MODE} if allow_umask_mode else {_SOCKET_MODE}
        if stat.S_IMODE(info.st_mode) not in allowed_modes:
            raise OSError("internal dispatch socket mode mismatch")


def _write_internal_server_status(
    state: str,
    *,
    error: str | None = None,
    detail: str | None = None,
) -> None:
    """Persist internal-server lifecycle state for the out-of-process CLI."""

    target = paths.get_internal_server_status_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "state": state,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if error is not None:
            payload["error"] = error
        if detail is not None:
            payload["detail"] = detail
        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(file_descriptor, 0o600)
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, separators=(",", ":"))
            file_descriptor = -1
            os.replace(temporary, target)
        except Exception:
            if file_descriptor >= 0:
                try:
                    os.close(file_descriptor)
                except OSError:
                    pass
            temporary.unlink(missing_ok=True)
            raise
    except OSError:
        logger.warning("could not persist internal dispatch server status", exc_info=True)


def start(controller: "Controller", *, socket_path: Optional[Path] = None) -> asyncio.Task:
    """Schedule the internal server to run on the controller's loop.

    Called from ``Controller.run`` once the loop is alive. Returns the
    background ``asyncio.Task`` so the caller can keep a handle for
    cancellation on shutdown.
    """

    loop = asyncio.get_event_loop()
    _write_internal_server_status("starting")
    task = loop.create_task(serve(controller, socket_path=socket_path), name="internal-dispatch-server")

    def _on_done(t: asyncio.Task) -> None:
        if t.cancelled():
            _write_internal_server_status("stopped")
            return
        exc = t.exception()
        if exc:
            logger.error("internal dispatch server exited with exception: %r", exc)
            _write_internal_server_status(
                "error",
                error="internal_server_unavailable",
                detail=str(exc)[:500],
            )
        else:
            _write_internal_server_status("stopped")

    task.add_done_callback(_on_done)
    return task


def note_stopped() -> None:
    """Record the server as stopped from a shutdown path.

    ``start``'s done callback is scheduled with ``call_soon``, so a shutdown
    that cancels the task and then closes the loop can finish before it runs.
    Shutdown calls this directly; a duplicate write from the callback is
    harmless because both record the same terminal state.
    """

    _write_internal_server_status("stopped")


# --- Internals --------------------------------------------------------


async def _safe_json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        body = {}
    return body if isinstance(body, dict) else {}


async def _build_dispatch_payload(payload: dict[str, Any]) -> tuple[str, MessageContext]:
    """Translate the JSON payload into a ``(text, MessageContext)`` pair.

    Raises ``ValueError`` with a caller-friendly message when the
    payload is missing required fields. The MessageContext defaults to
    ``platform="avibe"`` because the Web UI is the first / only caller;
    future CLI ``--sync`` callers will hand in their own platform.

    We also look up the workbench session's routing fields and copy
    them into ``platform_specific["agent_session_target"]`` /
    ``platform_specific["vibe_agent_name"]`` so ``MessageHandler``'s
    agent-selection branch picks up the Chat header's chosen agent /
    model / effort — matching the shape that scheduled tasks already
    feed in via ``core.scheduled_tasks`` so the handler stays one path.
    """

    text = payload.get("text")
    text = text if isinstance(text, str) else ""

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session_id is required")

    # A turn may be text-only, attachments-only (the agent reads the files), or
    # both. ``files`` are already-local web uploads resolved from media tokens.
    from core.workbench_media import file_attachments_from_specs

    files = file_attachments_from_specs(payload.get("files"))
    if not text.strip() and not files:
        raise ValueError("text or files is required")

    context = await asyncio.to_thread(
        _build_session_context,
        session_id,
        user_id=payload.get("user_id"),
        channel_id=payload.get("channel_id"),
        platform=payload.get("platform"),
        thread_id=payload.get("thread_id"),
        message_id=payload.get("message_id") or payload.get("user_message_id"),
        files=files,
        memory_cli_admitted=payload.get("memory_cli_admitted") is True,
        is_ordinary_text=payload.get("is_ordinary_text") is True,
    )
    if context.platform_specific is None:
        context.platform_specific = {}
    context.platform_specific.update(
        {
            "delivery_id": payload.get("user_message_id"),
            "scope_id": payload.get("scope_id"),
            "display_text": payload.get("display_text"),
            "message_content": payload.get("content"),
            "message_metadata": payload.get("metadata") or {},
            "author_id": payload.get("author_id"),
            "author_name": payload.get("author_name"),
        }
    )
    return text, context


def _build_session_context(
    session_id: str,
    *,
    user_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    platform: Optional[str] = None,
    thread_id: Optional[str] = None,
    message_id: Optional[str] = None,
    files: Optional[list] = None,
    memory_cli_admitted: bool = False,
    is_ordinary_text: bool = False,
) -> MessageContext:
    """Rebuild a Session's routing context from its durable scope and target.

    Shared by the dispatch endpoint and the cancel endpoint so a stop reuses
    the exact same session-routing context (chosen agent / model / effort,
    native session id, workdir) the turn ran under — that's what lets cancel
    reuse the IM ``/stop`` path to interrupt the right backend session.
    Workbench and IM Sessions use the same builder; Delivery hydration adds the
    exact sender and native Message identity afterward.
    """

    from core.scheduled_tasks import resolve_session_id_target

    target_info = resolve_session_id_target(session_id)
    session_row = _lookup_session(session_id)
    resolved_platform = platform or target_info.session_key.platform or "avibe"
    is_dm = target_info.session_key.scope_type == "user"
    resolved_channel_id = channel_id or (
        session_id
        if resolved_platform == "avibe"
        else target_info.session_key.scope_id
    )
    resolved_user_id = user_id or (
        target_info.session_key.scope_id if is_dm else "workbench"
    )
    platform_specific: dict[str, Any] = {
        "agent_session_id": session_id,
        "platform": resolved_platform,
        "is_dm": is_dm,
    }
    if resolved_platform == "avibe":
        platform_specific["workbench_session_id"] = session_id
    if memory_cli_admitted:
        platform_specific["memory_cli_admitted"] = True
    session_row = _lookup_session(session_id)
    if session_row is not None:
        target = {
            "id": session_row.get("id"),
            "agent_id": session_row.get("agent_id"),
            "agent_name": session_row.get("agent_name"),
            "agent_backend": session_row.get("agent_backend"),
            "agent_variant": session_row.get("agent_variant"),
            "model": session_row.get("model"),
            "reasoning_effort": session_row.get("reasoning_effort"),
            "native_session_id": session_row.get("native_session_id"),
            "workdir": session_row.get("workdir"),
            "metadata": session_row.get("metadata") or {},
            # Carry the stored anchor so SessionHandler.get_base_session_id reuses it
            # instead of computing ``avibe_<id>`` — otherwise, after a restart, new
            # dispatches look up the native-session map under the wrong anchor and
            # start a fresh backend thread for the same Chat session (Codex P2).
            "session_anchor": session_row.get("session_anchor"),
        }
        platform_specific["agent_session_target"] = target
        platform_specific["suppress_delivery"] = session_row.get("visibility") == "background"
        if session_row.get("agent_name"):
            platform_specific["vibe_agent_name"] = session_row["agent_name"]

    return MessageContext(
        user_id=str(resolved_user_id),
        channel_id=str(resolved_channel_id),
        platform=resolved_platform,
        thread_id=thread_id or target_info.session_key.thread_id,
        message_id=message_id,
        platform_specific=platform_specific,
        files=files,
        is_ordinary_text=is_ordinary_text,
    )


def _lookup_session(session_id: str) -> Optional[dict[str, Any]]:
    """Load the workbench session row for routing metadata.

    Failures are swallowed and logged: the dispatch still proceeds with
    default routing rather than 5xx'ing the SSE stream. The session
    *not existing* is a real caller error but
    ``MessageHandler._handle_turn`` already produces a meaningful error
    in that case.
    """

    try:
        from core.services import sessions as sessions_service

        engine = get_cached_sqlite_engine()
        with engine.connect() as conn:
            return sessions_service.get_session(conn, session_id)
    except LookupError:
        return None
    except Exception:
        logger.exception("internal_server: failed to load session metadata for %s", session_id)
        return None


def _sse_event(event_type: str, data: Any) -> str:
    """Format one SSE chunk. Each chunk is a single ``event:``/``data:``
    pair separated by the spec-mandated blank line.
    """

    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
