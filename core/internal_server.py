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
import re
import socket
import stat
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional, TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.exc import IntegrityError

from config import paths
from config.atomic_io import write_atomic
from core.delivery_target import normalize_message_kind
from core.memory_loader import MEMORY_LIST_CURSOR_MAX_BYTES
from core.services.dispatch import SOURCE_HUMAN, SOURCE_SCHEDULED
from modules.im.base import MessageContext
from storage.db import get_cached_sqlite_engine
from vibe.memory_contract import (
    MemoryImplementationIncompatibleError,
    MemoryImplementationUnavailableError,
    MemoryStoreUnavailableError,
)
from vibe.message_identity import HARNESS_TYPE, is_input_turn

if TYPE_CHECKING:  # pragma: no cover - typing only
    from core.controller import Controller

logger = logging.getLogger(__name__)


def _memory_implementation_error_code(error: BaseException) -> str:
    return (
        "memory_implementation_incompatible"
        if isinstance(error, MemoryImplementationIncompatibleError)
        else "memory_implementation_unavailable"
    )
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
_PROCESSING_RECORD_CURSOR_RE = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
_PROCESSING_RECORD_ENTRY_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z")


def _processing_record_list_query(
    request: Request,
) -> tuple[str | None, int, str | None]:
    items = list(request.query_params.multi_items())
    keys = [key for key, _value in items]
    if any(key not in {"cursor", "limit", "project"} for key in keys) or len(
        keys
    ) != len(set(keys)):
        raise ValueError("invalid Processing Record query")
    values = dict(items)
    cursor = values.get("cursor")
    if cursor is not None and _PROCESSING_RECORD_CURSOR_RE.fullmatch(cursor) is None:
        raise ValueError("invalid Processing Record cursor")
    raw_limit = values.get("limit", "20")
    if not raw_limit.isascii() or not raw_limit.isdecimal():
        raise ValueError("invalid Processing Record limit")
    limit = int(raw_limit)
    if not 1 <= limit <= 50:
        raise ValueError("invalid Processing Record limit")
    project = values.get("project")
    if project is not None:
        from vibe.memory_project_ids import parse_agent_search_project

        project = parse_agent_search_project(project)
    return cursor, limit, project


def _processing_record_entry_query(request: Request) -> tuple[str, str | None]:
    items = list(request.query_params.multi_items())
    keys = [key for key, _value in items]
    if (
        any(key not in {"memcell_id", "project"} for key in keys)
        or len(keys) != len(set(keys))
        or "memcell_id" not in keys
    ):
        raise ValueError("invalid Processing Record entry query")
    values = dict(items)
    memcell_id = values["memcell_id"]
    if _PROCESSING_RECORD_ENTRY_ID_RE.fullmatch(memcell_id) is None:
        raise ValueError("invalid Processing Record entry id")
    project = values.get("project")
    if project is not None:
        from vibe.memory_project_ids import parse_agent_search_project

        project = parse_agent_search_project(project)
    return memcell_id, project


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
        from vibe.memory_ui_access import process_ui_read_secret

        memory_ui_secret = process_ui_read_secret()
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
    show_access_write_lock = asyncio.Lock()
    app.state.show_access_write_lock = show_access_write_lock

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

        The explicit source intent decides whether this content queues or steers.
        Content-bearing P1 targets this Delivery; content-free P1 promotion is
        exposed separately through ``SessionTurnManager.send_now``.
        The submission remains a Delivery until exact native acceptance materializes
        the harness Message.
        """
        if not session_id:
            submission = await manager.submit(None, context, text, source=SOURCE_SCHEDULED)
            return submission.route

        from core.message_mirror import _scope_id_for_session
        from core.message_priority import normalize_delivery_intent
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
        legacy_send_now = str(delivery_intent or "").strip().lower() == "send_now"
        delivery_intent = normalize_delivery_intent(delivery_intent)
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
                        if (
                            legacy_send_now
                            and existing_state == "queued"
                            and existing.get("priority") == "p3"
                            and message_deliveries.delivery_has_history_event(
                                existing,
                                kind="admission",
                            )
                            and not message_deliveries.delivery_has_history_event(
                                existing,
                                kind="steer",
                            )
                        ):
                            existing = message_deliveries.cas_delivery(
                                conn,
                                str(existing["id"]),
                                expected_version=int(existing["version"]),
                                expected_states=("queued",),
                                values={"priority": "p1", "state": "reserved"},
                                history_event={
                                    "kind": "legacy_send_now_re_admission",
                                    "from_priority": "p3",
                                },
                            )
                            if existing is None:
                                raise RuntimeError(
                                    "legacy send-now Delivery re-admission lost"
                                )
                            delivery_owner_transferred = True
                        else:
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
                if not delivery_owner_transferred:
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

                persisted_intent = delivery_intent_for_priority(
                    delivery_request.priority
                )
                effective_delivery_intent = persisted_intent
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
            if execution_id and not delivery_owner_transferred:
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

        def _defer_transferred_delivery() -> TurnSubmissionResult:
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

        try:
            result = await manager.deliver(
                delivery_request,
                context=context,
            )
        except asyncio.CancelledError:
            if not delivery_owner_transferred:
                raise
            # Once the Run points at this Delivery, the executor wrapper is no
            # longer its lifecycle owner. In particular, cancellation can race
            # a shielded native P1 write: the adapter may have accepted the
            # steer even though admission has not projected that receipt yet.
            # Leave the durable owner recoverable instead of letting the wrapper
            # settle the Run before the native turn reaches terminal.
            logger.warning(
                "Delivery admission canceled after Agent Run ownership transfer; "
                "deferring to recovery: run=%s delivery=%s",
                execution_id,
                delivery_id,
            )
            return _defer_transferred_delivery()
        except Exception:
            if not delivery_owner_transferred:
                raise
            logger.exception(
                "Delivery admission deferred to recovery after Agent Run ownership "
                "transfer: run=%s delivery=%s",
                execution_id,
                delivery_id,
            )
            return _defer_transferred_delivery()
        delivery_state = str(result.state)
        route = "enqueued" if delivery_state in {
            "queued",
            "pending_steer",
            "steering",
            "interrupt_waiting",
            "waiting_terminal",
            "reconciling_steer",
        } else "ran"
        submission = TurnSubmissionResult(
            route=route,
            queue_persisted=True,
            target_was_busy=target_was_busy,
            delivery_status=delivery_state,
            delivery_owner_transferred=delivery_owner_transferred,
        )
        _publish_scheduled_queue_growth(session_id, delivery_state)
        if delivery_owner_transferred and execution_id:
            try:
                with run_update_event_transaction(get_cached_sqlite_engine()) as conn:
                    recorded = record_agent_run_delivery_outcome_in_connection(
                        conn,
                        execution_id,
                        {
                            "intent": effective_delivery_intent,
                            "status": delivery_state,
                            "target_was_busy": submission.target_was_busy,
                        },
                    )
                if not recorded:
                    logger.warning(
                        "Agent Run Delivery outcome lost its exact CAS after ownership "
                        "transfer: run=%s delivery=%s",
                        execution_id,
                        delivery_id,
                    )
            except Exception:
                logger.exception(
                    "Agent Run Delivery outcome persistence deferred after ownership "
                    "transfer: run=%s delivery=%s",
                    execution_id,
                    delivery_id,
                )
        return submission if delivery_owner_transferred else route

    @app.get("/internal/health")
    async def _health() -> dict[str, Any]:
        return {"ok": True, "service": "vibe-remote-internal", "version": 1}

    @app.post("/internal/show-access/settings-read")
    async def _show_access_settings_read(request: Request) -> Any:
        from core.show_pages import ShowPageError, ShowPageStore, show_access_payload

        payload = await _safe_json(request)
        if set(payload) != {"page_id"} or not isinstance(payload.get("page_id"), str):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "invalid_show_access_settings_request"},
            )
        page_id = payload["page_id"]

        def _read() -> dict[str, Any]:
            store = ShowPageStore()
            try:
                show_access = store.get_access(page_id)
            finally:
                store.close()
            if show_access is None:
                raise ShowPageError(
                    "This session has no Show Page.",
                    code="show_page_not_found",
                )
            if show_access.page_id != page_id:
                raise RuntimeError("show_access_page_identity_mismatch")
            return {"show_access": show_access_payload(show_access)}

        try:
            return await asyncio.to_thread(_read)
        except ShowPageError as exc:
            status = 404 if exc.code == "show_page_not_found" else 400
            return JSONResponse(
                status_code=status,
                content={"ok": False, "error": exc.code},
            )
        except Exception:
            logger.exception("internal ShowAccess settings read failed")
            return JSONResponse(
                status_code=500,
                content={"ok": False, "error": "show_access_internal_failure"},
            )

    @app.post("/internal/show-access/apply")
    async def _show_access_apply(request: Request) -> Any:
        from core.show_pages import (
            ShowPageError,
            ShowPageStore,
            parse_show_access_apply_request,
            show_access_payload,
        )

        payload = await _safe_json(request)
        parsed = parse_show_access_apply_request(payload)
        if parsed is None:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "invalid_show_access_apply_request"},
            )
        page_id = parsed["page_id"]

        def _apply() -> dict[str, Any]:
            store = ShowPageStore()
            try:
                apply_kwargs = {
                    "expected_revision": parsed["expected_revision"],
                    "target_access_mode": parsed["target_access_mode"],
                    "target_share_id": parsed["target_share_id"],
                }
                if "target_emails" in parsed:
                    apply_kwargs["target_emails"] = parsed["target_emails"]
                else:
                    apply_kwargs["target_entries"] = parsed["target_entries"]
                result = store.apply_access(page_id, **apply_kwargs)
            finally:
                store.close()
            if result.show_access.page_id != page_id:
                raise RuntimeError("show_access_page_identity_mismatch")
            return {
                "status": result.status,
                "show_access": show_access_payload(result.show_access),
            }

        try:
            async with show_access_write_lock:
                return await asyncio.to_thread(_apply)
        except ShowPageError as exc:
            status = 404 if exc.code == "show_page_not_found" else 400
            return JSONResponse(
                status_code=status,
                content={"ok": False, "error": exc.code},
            )
        except Exception:
            logger.exception("internal ShowAccess apply failed")
            return JSONResponse(
                status_code=500,
                content={"ok": False, "error": "show_access_internal_failure"},
            )

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

    @app.post("/internal/running-agents/snapshot")
    async def _running_agents_snapshot(request: Request) -> Any:
        """Return live agents plus ownership for one bounded Run candidate set."""

        from core.services.running_agents import (
            HARNESS_OWNERSHIP_CANDIDATE_LIMIT,
            snapshot_running_agents,
        )

        payload = await _safe_json(request)
        run_ids = payload.get("run_ids") if isinstance(payload, dict) else None
        if not isinstance(run_ids, list) or len(run_ids) > HARNESS_OWNERSHIP_CANDIDATE_LIMIT:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "invalid_run_candidates"},
            )
        return await asyncio.to_thread(
            snapshot_running_agents,
            controller,
            ownership_candidate_run_ids=run_ids,
        )

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
            "message_kind": delivery_payload.get("message_kind"),
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

    @app.post("/internal/invalidate-activity-streaming")
    async def _invalidate_activity_streaming() -> Any:
        """Drop the controller process's cached Agent Activity display flag."""
        try:
            from core.message_mirror import reset_activity_flag_cache

            reset_activity_flag_cache()
            return JSONResponse(status_code=200, content={"ok": True})
        except Exception as exc:
            logger.exception("internal Agent Activity cache invalidation failed")
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

    @app.post("/internal/backend-auth/test")
    async def _test_backend_auth(request: Request) -> Any:
        """Probe credentials through the controller-owned Agent runtime."""
        payload = await _safe_json(request)
        backend = payload.get("backend")
        model = payload.get("model")
        if not isinstance(backend, str) or not backend.strip():
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "backend must be a non-empty string"},
            )
        if model is not None and not isinstance(model, str):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "model must be a string"},
            )
        service = getattr(controller, "agent_auth_service", None)
        test = getattr(service, "test_web_auth", None)
        if not callable(test):
            return JSONResponse(
                status_code=503,
                content={"ok": False, "error": "backend_runtime_unavailable"},
            )
        try:
            result = await test(
                backend.strip().lower(),
                model=model.strip() if isinstance(model, str) and model.strip() else None,
            )
            return JSONResponse(status_code=200, content=result)
        except Exception as exc:
            logger.exception("internal backend auth test failed")
            return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})
    @app.post("/internal/reconcile-memory")
    async def _reconcile_memory() -> Any:
        """Hot-apply persisted Memory configuration on the controller loop."""

        try:
            from config.v2_config import V2Config

            config = await asyncio.to_thread(V2Config.load)
            result = await controller.reconcile_memory(config.memory)
            return JSONResponse(status_code=200, content=result)
        except ImportError:
            return JSONResponse(
                status_code=503,
                content={"ok": False, "error": "memory_implementation_unavailable"},
            )
        except Exception as exc:
            if type(exc).__name__ == "MemoryRuntimeActivationError":
                logger.exception("internal memory runtime activation failed during reconcile")
                return JSONResponse(
                    status_code=503,
                    content={"ok": False, "error": "memory_runtime_install_failed"},
                )
            if isinstance(exc, (MemoryImplementationUnavailableError, MemoryImplementationIncompatibleError)):
                return JSONResponse(
                    status_code=503,
                    content={"ok": False, "error": _memory_implementation_error_code(exc)},
                )
            logger.exception("internal memory reconcile failed")
            return JSONResponse(
                status_code=503,
                content={"ok": False, "error": "memory_reconcile_failed"},
            )

    @app.post("/internal/memory/wake")
    async def _memory_wake() -> Any:
        """Non-destructively wake the existing admitted Memory root."""

        try:
            result = await controller.wake_memory()
        except MemoryStoreUnavailableError:
            result = {
                "ok": False,
                "state": "degraded",
                "error": "memory_store_unavailable",
            }
        except (MemoryImplementationUnavailableError, MemoryImplementationIncompatibleError) as exc:
            result = {"ok": False, "state": "degraded", "error": _memory_implementation_error_code(exc)}
        except Exception:
            logger.exception("internal memory wake failed")
            result = {"ok": False, "state": "degraded", "error": "memory_wake_failed"}
        status_code = 200 if result.get("ok") is True else (
            409 if result.get("error") == "memory_operation_in_progress" else 503
        )
        return JSONResponse(status_code=status_code, content=result)

    async def _confirmed_memory_data_operation(
        request: Request,
        *,
        operation: str,
    ) -> Any:
        if _verified_memory_ui_user_key(request) is None:
            return JSONResponse(
                status_code=403,
                content={"ok": False, "operation": operation, "error": "memory_access_denied"},
            )
        payload = await _safe_json(request)
        if payload != {"confirm_loss": True}:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "operation": operation,
                    "error": "memory_loss_confirmation_required",
                    "result": "unchanged",
                },
            )
        handler = (
            getattr(controller, "repair_memory", None)
            if operation == "repair"
            else getattr(controller, "delete_memory_data", None)
        )
        if not callable(handler):
            return JSONResponse(
                status_code=503,
                content={"ok": False, "operation": operation, "error": "memory_runtime_missing"},
            )
        try:
            result = await handler(confirm_loss=True)
        except (MemoryImplementationUnavailableError, MemoryImplementationIncompatibleError) as exc:
            result = {
                "ok": False,
                "operation": operation,
                "error": _memory_implementation_error_code(exc),
                "result": "failed",
            }
        except Exception:
            logger.exception("internal Memory %s failed", operation)
            result = {
                "ok": False,
                "operation": operation,
                "error": f"memory_{operation}_failed",
                "result": "failed",
            }
        if result.get("ok") is True:
            status_code = 200
        elif result.get("error") in {
            "memory_operation_in_progress",
            "memory_repair_not_required",
        }:
            status_code = 409
        else:
            status_code = 503
        return JSONResponse(status_code=status_code, content=result)

    @app.post("/internal/memory/repair")
    async def _memory_repair(request: Request) -> Any:
        return await _confirmed_memory_data_operation(request, operation="repair")

    @app.post("/internal/memory/delete-data")
    async def _memory_delete_data(request: Request) -> Any:
        return await _confirmed_memory_data_operation(request, operation="delete_data")

    @app.post("/internal/memory/reconfigure")
    async def _memory_reconfigure(request: Request) -> Any:
        if _verified_memory_ui_user_key(request) is None:
            return JSONResponse(
                status_code=403,
                content={"ok": False, "operation": "reconfigure", "error": "memory_access_denied"},
            )
        payload = await _safe_json(request)
        if not isinstance(payload, dict) or set(payload) != {
            "confirm_loss",
            "memory",
            "expected_memory",
        }:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "operation": "reconfigure",
                    "error": "memory_loss_confirmation_required",
                },
            )
        if payload.get("confirm_loss") is not True:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "operation": "reconfigure",
                    "error": "memory_loss_confirmation_required",
                },
            )
        try:
            from config.v2_config import memory_config_from_payload

            candidate = memory_config_from_payload(payload["memory"])
            expected = memory_config_from_payload(payload["expected_memory"])
            result = await controller.reconfigure_memory(
                candidate,
                expected_config=expected,
                confirm_loss=True,
            )
        except (TypeError, ValueError):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "operation": "reconfigure", "error": "memory_invalid_input"},
            )
        except (MemoryImplementationUnavailableError, MemoryImplementationIncompatibleError) as exc:
            result = {
                "ok": False,
                "operation": "reconfigure",
                "error": _memory_implementation_error_code(exc),
            }
        except Exception:
            logger.exception("internal Memory reconfigure failed")
            result = {
                "ok": False,
                "operation": "reconfigure",
                "error": "memory_reconfigure_failed",
            }
        status_code = 200 if result.get("ok") is True else (
            409 if result.get("error") == "memory_operation_in_progress" else 503
        )
        return JSONResponse(status_code=status_code, content=result)

    @app.post("/internal/memory/install-runtime")
    async def _memory_install_runtime() -> Any:
        """Install or repair the managed runtime on the controller lifecycle."""

        try:
            result = await controller.install_memory_runtime()
            return JSONResponse(status_code=200, content=result)
        except (MemoryImplementationUnavailableError, MemoryImplementationIncompatibleError) as exc:
            return JSONResponse(
                status_code=503,
                content={"ok": False, "reason": _memory_implementation_error_code(exc)},
            )
        except Exception:
            logger.exception("internal memory runtime install failed")
            return JSONResponse(status_code=503, content={"ok": False, "reason": "memory_runtime_install_failed"})

    @app.post("/internal/memory/preflight")
    async def _memory_preflight(request: Request) -> Any:
        if _verified_memory_ui_user_key(request) is None:
            return JSONResponse(status_code=403, content={"ok": False, "error": "memory_access_denied"})
        payload = await _safe_json(request)
        try:
            from config.v2_config import memory_config_from_payload

            config = memory_config_from_payload(payload.get("memory", payload) if isinstance(payload, dict) else {})
            preflight = getattr(controller, "preflight_memory", None)
            if not callable(preflight):
                return JSONResponse(status_code=503, content={"ok": False, "error": "memory_runtime_missing"})
            return JSONResponse(status_code=200, content=await preflight(config))
        except (MemoryImplementationUnavailableError, MemoryImplementationIncompatibleError) as exc:
            return JSONResponse(
                status_code=503,
                content={"ok": False, "error": _memory_implementation_error_code(exc)},
            )
        except Exception:
            logger.exception("internal memory preflight failed")
            return JSONResponse(status_code=503, content={"ok": False, "error": "memory_processing_failed"})

    def _memory_cli_scope(request: Request) -> tuple[str, str] | None:
        from vibe.memory_http_headers import CALLER_SESSION_HEADER

        session_id = str(request.headers.get(CALLER_SESSION_HEADER) or "").strip()
        if not session_id:
            return None
        resolve = getattr(controller, "memory_scope_for_cli_session", None)
        scope = resolve(session_id) if callable(resolve) else None
        if not bool(getattr(getattr(getattr(controller, "config", None), "memory", None), "enabled", False)):
            return None
        implementation_sessions = getattr(controller, "_memory_implementation_cli_sessions", None)
        if (
            getattr(controller, "_memory_implementation_error", None) is not None
            and isinstance(implementation_sessions, set)
            and session_id in implementation_sessions
            and isinstance(scope, tuple)
            and len(scope) == 2
        ):
            return scope
        from vibe.memory_contract import is_memory_principal_id
        from vibe.memory_project_ids import is_project_id

        if (
            isinstance(scope, tuple)
            and len(scope) == 2
            and is_memory_principal_id(scope[0])
            and is_project_id(scope[1])
        ):
            return scope
        return None

    @app.post("/internal/memory/archive-session")
    async def _memory_archive_session(request: Request) -> Any:
        """Archive one Workbench session through the controller-owned write."""

        payload = await _safe_json(request)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"session_id"}
            or not isinstance(payload.get("session_id"), str)
            or not payload["session_id"]
            or payload["session_id"] != payload["session_id"].strip()
        ):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "memory_invalid_input"},
            )
        archive_session = getattr(controller, "archive_session", None)
        if not callable(archive_session):
            return JSONResponse(
                status_code=503,
                content={"ok": False, "error": "session_archive_unavailable"},
            )
        try:
            session = await archive_session(
                payload["session_id"],
                deadline_seconds=5.0,
            )
        except LookupError:
            return JSONResponse(
                status_code=404,
                content={"ok": False, "error": "session_not_found"},
            )
        except PermissionError as error:
            return JSONResponse(
                status_code=403,
                content={
                    "ok": False,
                    "error": getattr(error, "code", "forbidden"),
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug(
                "internal Workbench session archive failed for %s",
                payload["session_id"],
                exc_info=True,
            )
            return JSONResponse(
                status_code=503,
                content={"ok": False, "error": "session_archive_unavailable"},
            )
        if not isinstance(session, dict):
            return JSONResponse(
                status_code=503,
                content={"ok": False, "error": "session_archive_unavailable"},
            )
        return {"ok": True, "session": session}

    def _verified_memory_ui_user_key(request: Request) -> str | None:
        from vibe.memory_http_headers import (
            CALLER_SESSION_HEADER,
            MEMORY_USER_KEY_HEADER,
        )

        session_id = str(request.headers.get(CALLER_SESSION_HEADER) or "").strip()
        user_key = str(request.headers.get(MEMORY_USER_KEY_HEADER) or "").strip()
        remote_prefix = "avibe:remote:"
        if session_id or not (
            user_key == "avibe:local"
            or (user_key.startswith(remote_prefix) and len(user_key) > len(remote_prefix))
        ):
            return None
        from vibe.memory_ui_access import MEMORY_UI_PROOF_HEADER, verify_ui_read_proof

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

    def _memory_read_owner(
        request: Request,
    ) -> tuple[str | None, tuple[str, str] | None] | None:
        from vibe.memory_http_headers import MEMORY_USER_KEY_HEADER

        if str(request.headers.get(MEMORY_USER_KEY_HEADER) or "").strip():
            user_key = _verified_memory_ui_user_key(request)
            if user_key is None:
                return None
            return user_key, None
        scope = _memory_cli_scope(request)
        return (None, scope) if scope is not None else None

    @app.get("/internal/memory/status")
    async def _memory_status() -> Any:
        try:
            return await controller.memory_status_payload()
        except (MemoryImplementationUnavailableError, MemoryImplementationIncompatibleError) as exc:
            return JSONResponse(status_code=503, content={"error": _memory_implementation_error_code(exc)})
        except Exception:
            logger.warning("internal memory status failed")
            return JSONResponse(status_code=503, content={"error": "memory_store_unavailable"})

    @app.get("/internal/memory/processing-record")
    async def _memory_processing_record(request: Request) -> Any:
        try:
            return await controller.memory_processing_record_payload(
                verified_user_key=_verified_memory_ui_user_key(request)
            )
        except (MemoryImplementationUnavailableError, MemoryImplementationIncompatibleError) as exc:
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": _memory_implementation_error_code(exc)},
            )
        except Exception:
            logger.warning("internal memory Processing Record read failed")
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": "memory_store_unavailable"},
            )

    @app.get("/internal/memory/failures")
    async def _memory_failures(request: Request) -> Any:
        try:
            return await controller.memory_failure_log_payload(
                verified_user_key=_verified_memory_ui_user_key(request)
            )
        except (MemoryImplementationUnavailableError, MemoryImplementationIncompatibleError) as exc:
            return JSONResponse(status_code=503, content={"error": _memory_implementation_error_code(exc)})
        except Exception:
            logger.warning("internal memory failure log failed")
            return JSONResponse(status_code=503, content={"error": "memory_store_unavailable"})

    @app.get("/internal/memory/maintenance")
    async def _memory_maintenance(request: Request) -> Any:
        try:
            return await controller.memory_maintenance_payload(
                verified_user_key=_verified_memory_ui_user_key(request)
            )
        except (MemoryImplementationUnavailableError, MemoryImplementationIncompatibleError) as exc:
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": _memory_implementation_error_code(exc)},
            )
        except Exception:
            logger.warning("internal memory maintenance read failed")
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": "memory_store_unavailable"},
            )

    @app.get("/internal/memory/profile")
    async def _memory_profile(request: Request) -> Any:
        owner = _memory_read_owner(request)
        if owner is None:
            return JSONResponse(status_code=403, content={"status": "failed", "error": "memory_access_denied"})
        verified_user_key, cli_scope = owner
        try:
            return await controller.memory_profile_payload(
                verified_user_key=verified_user_key,
                cli_scope=cli_scope,
            )
        except MemoryStoreUnavailableError:
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": "memory_store_unavailable"},
            )
        except (MemoryImplementationUnavailableError, MemoryImplementationIncompatibleError) as exc:
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": _memory_implementation_error_code(exc)},
            )
        except PermissionError:
            return JSONResponse(status_code=403, content={"status": "failed", "error": "memory_access_denied"})
        except Exception:
            logger.warning("internal memory profile failed")
            return JSONResponse(status_code=503, content={"status": "failed", "error": "memory_processing_failed"})

    @app.get("/internal/memory/processing-record/entries")
    async def _memory_processing_record_entries(request: Request) -> Any:
        try:
            cursor, limit, requested_project = _processing_record_list_query(request)
            owner = _memory_read_owner(request)
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"status": "failed", "error": "memory_invalid_input"},
            )
        if owner is None:
            return JSONResponse(
                status_code=403,
                content={"status": "failed", "error": "memory_access_denied"},
            )
        verified_user_key, cli_scope = owner
        try:
            return await controller.memory_processing_record_entries_payload(
                cursor=cursor,
                limit=limit,
                project_id=requested_project,
                verified_user_key=verified_user_key,
                cli_scope=cli_scope,
            )
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"status": "failed", "error": "memory_invalid_input"},
            )
        except MemoryStoreUnavailableError:
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": "memory_store_unavailable"},
            )
        except (MemoryImplementationUnavailableError, MemoryImplementationIncompatibleError) as exc:
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": _memory_implementation_error_code(exc)},
            )
        except PermissionError:
            return JSONResponse(
                status_code=403,
                content={"status": "failed", "error": "memory_access_denied"},
            )
        except Exception:
            logger.warning("internal native Processing Record list failed")
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": "memory_processing_failed"},
            )

    @app.get("/internal/memory/processing-record/entry")
    async def _memory_processing_record_entry(request: Request) -> Any:
        try:
            memcell_id, requested_project = _processing_record_entry_query(request)
            owner = _memory_read_owner(request)
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"status": "failed", "error": "memory_invalid_input"},
            )
        if owner is None:
            return JSONResponse(
                status_code=403,
                content={"status": "failed", "error": "memory_access_denied"},
            )
        verified_user_key, cli_scope = owner
        try:
            payload = await controller.memory_processing_record_entry_payload(
                memcell_id=memcell_id,
                project_id=requested_project,
                verified_user_key=verified_user_key,
                cli_scope=cli_scope,
            )
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"status": "failed", "error": "memory_invalid_input"},
            )
        except MemoryStoreUnavailableError:
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": "memory_store_unavailable"},
            )
        except (MemoryImplementationUnavailableError, MemoryImplementationIncompatibleError) as exc:
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": _memory_implementation_error_code(exc)},
            )
        except PermissionError:
            return JSONResponse(
                status_code=403,
                content={"status": "failed", "error": "memory_access_denied"},
            )
        except Exception:
            logger.warning("internal native Processing Record detail failed")
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": "memory_processing_failed"},
            )
        if payload.get("status") == "not_found":
            return JSONResponse(
                status_code=404,
                content={
                    "status": "failed",
                    "error": "memory_processing_record_entry_not_found",
                },
            )
        return payload

    @app.get("/internal/memory/projects")
    async def _memory_projects(request: Request) -> Any:
        owner = _memory_read_owner(request)
        if owner is None:
            return JSONResponse(
                status_code=403,
                content={"status": "failed", "error": "memory_access_denied"},
            )
        verified_user_key, cli_scope = owner
        try:
            return await controller.memory_projects_payload(
                verified_user_key=verified_user_key,
                cli_scope=cli_scope,
            )
        except (MemoryImplementationUnavailableError, MemoryImplementationIncompatibleError) as exc:
            logger.warning("internal memory project list failed")
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": _memory_implementation_error_code(exc)},
            )
        except MemoryStoreUnavailableError:
            logger.warning("internal memory project list failed")
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": "memory_store_unavailable"},
            )
        except PermissionError:
            return JSONResponse(
                status_code=403,
                content={"status": "failed", "error": "memory_access_denied"},
            )
        except Exception:
            logger.warning("internal memory project list failed")
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": "memory_store_unavailable"},
            )

    @app.post("/internal/memory/search")
    async def _memory_search(request: Request) -> Any:
        owner = _memory_read_owner(request)
        if owner is None:
            return JSONResponse(status_code=403, content={"status": "failed", "error": "memory_access_denied"})
        verified_user_key, cli_scope = owner
        payload = await _safe_json(request)
        if (
            not isinstance(payload, dict)
            or not {"query", "policy"}.issubset(payload)
            or set(payload) - {"query", "policy", "project"}
            or not isinstance(payload.get("query"), str)
        ):
            return JSONResponse(status_code=400, content={"status": "failed", "error": "memory_invalid_input"})
        from vibe.memory_http_headers import CALLER_SESSION_HEADER, MEMORY_USER_KEY_HEADER
        from vibe.memory_project_ids import (
            omitted_project_to_default,
            parse_agent_search_project,
            parse_ui_search_project,
        )
        try:
            from vibe.memory_contract import RecallPolicy

            policy = RecallPolicy.from_payload(payload.get("policy"))
            raw_project = omitted_project_to_default(payload.get("project"))
            if str(request.headers.get(MEMORY_USER_KEY_HEADER) or "").strip():
                project_id = parse_ui_search_project(raw_project)
            else:
                project_id = parse_agent_search_project(raw_project)
        except (TypeError, ValueError):
            return JSONResponse(status_code=400, content={"status": "failed", "error": "memory_invalid_input"})
        except ImportError:
            return JSONResponse(status_code=503, content={"status": "failed", "error": "memory_implementation_unavailable"})
        current_session_id = str(request.headers.get(CALLER_SESSION_HEADER) or "").strip() or None
        try:
            return await controller.memory_search_payload(
                query=payload["query"],
                policy=policy,
                project_id=project_id,
                current_session_id=current_session_id,
                verified_user_key=verified_user_key,
                cli_scope=cli_scope,
            )
        except ValueError:
            return JSONResponse(status_code=400, content={"status": "failed", "error": "memory_invalid_input"})
        except MemoryStoreUnavailableError:
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": "memory_store_unavailable"},
            )
        except (MemoryImplementationUnavailableError, MemoryImplementationIncompatibleError) as exc:
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": _memory_implementation_error_code(exc)},
            )
        except PermissionError:
            return JSONResponse(status_code=403, content={"status": "failed", "error": "memory_access_denied"})
        except Exception:
            logger.warning("internal memory search failed")
            return JSONResponse(status_code=503, content={"status": "failed", "error": "memory_processing_failed"})

    @app.post("/internal/memory/list")
    async def _memory_list(request: Request) -> Any:
        owner = _memory_read_owner(request)
        if owner is None:
            return JSONResponse(
                status_code=403,
                content={"status": "failed", "error": "memory_access_denied"},
            )
        verified_user_key, cli_scope = owner
        payload = await _safe_json(request)
        if not isinstance(payload, dict) or set(payload) - {
            "project",
            "page",
            "limit",
            "cursor",
            "origin",
        }:
            return JSONResponse(
                status_code=400,
                content={"status": "failed", "error": "memory_invalid_input"},
            )
        from vibe.memory_http_headers import MEMORY_USER_KEY_HEADER
        from vibe.memory_project_ids import (
            MEMORY_SEARCH_ALL_PROJECTS,
            omitted_project_to_default,
            parse_agent_search_project,
            parse_ui_search_project,
        )
        from core.memory_loader import MEMORY_LIST_CURSOR_MAX_BYTES
        from vibe.memory_contract import MAX_MEMORY_LIST_PAGE_SIZE
        is_ui = bool(str(request.headers.get(MEMORY_USER_KEY_HEADER) or "").strip())
        limit = payload.get("limit", 20)
        origin = payload.get("origin", "user")
        try:
            raw_project = omitted_project_to_default(payload.get("project"))
            project_id = (
                parse_ui_search_project(raw_project)
                if is_ui
                else parse_agent_search_project(raw_project)
            )
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"status": "failed", "error": "memory_invalid_input"},
            )
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_MEMORY_LIST_PAGE_SIZE
            or origin not in ("user", "agent")
            or ("origin" in payload and not is_ui)
        ):
            return JSONResponse(
                status_code=400,
                content={"status": "failed", "error": "memory_invalid_input"},
            )
        if project_id == MEMORY_SEARCH_ALL_PROJECTS:
            cursor = payload.get("cursor")
            page = None
            try:
                cursor_bytes = (
                    len(cursor.encode("utf-8"))
                    if isinstance(cursor, str)
                    else None
                )
            except UnicodeEncodeError:
                cursor_bytes = MEMORY_LIST_CURSOR_MAX_BYTES + 1
            if (
                "page" in payload
                or (
                    cursor is not None
                    and (
                        not isinstance(cursor, str)
                        or not cursor
                        or cursor_bytes is None
                        or cursor_bytes > MEMORY_LIST_CURSOR_MAX_BYTES
                    )
                )
            ):
                return JSONResponse(
                    status_code=400,
                    content={"status": "failed", "error": "memory_invalid_input"},
                )
        else:
            cursor = None
            page = payload.get("page", 1)
            if (
                "cursor" in payload
                or isinstance(page, bool)
                or not isinstance(page, int)
                or page < 1
            ):
                return JSONResponse(
                    status_code=400,
                    content={"status": "failed", "error": "memory_invalid_input"},
                )
        try:
            result = await controller.memory_list_payload(
                project_id=project_id,
                page=page,
                cursor=cursor,
                limit=limit,
                origin=origin if "origin" in payload else None,
                verified_user_key=verified_user_key,
                cli_scope=cli_scope,
            )
            if result == {
                "status": "failed",
                "error": "memory_invalid_input",
            }:
                return JSONResponse(status_code=400, content=result)
            return result
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"status": "failed", "error": "memory_invalid_input"},
            )
        except MemoryStoreUnavailableError:
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": "memory_store_unavailable"},
            )
        except (MemoryImplementationUnavailableError, MemoryImplementationIncompatibleError) as exc:
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": _memory_implementation_error_code(exc)},
            )
        except PermissionError:
            return JSONResponse(
                status_code=403,
                content={"status": "failed", "error": "memory_access_denied"},
            )
        except Exception:
            logger.warning("internal memory list failed")
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": "memory_processing_failed"},
            )

    @app.post("/internal/memory/remember")
    async def _memory_remember(request: Request) -> Any:
        scope = _memory_cli_scope(request)
        if scope is None:
            return JSONResponse(status_code=403, content={"status": "failed", "error": "memory_access_denied"})
        principal_id, project_id = scope
        payload = await _safe_json(request)
        if (
            not isinstance(payload, dict)
            or not {"text"}.issubset(payload)
            or set(payload) - {"text", "project"}
            or not isinstance(payload.get("text"), str)
            or not payload["text"].strip()
        ):
            return JSONResponse(status_code=400, content={"status": "failed", "error": "memory_invalid_input"})
        from vibe.memory_project_ids import omitted_project_to_default, parse_writable_memory_project

        try:
            project_id = parse_writable_memory_project(
                omitted_project_to_default(payload.get("project"))
            )
        except ValueError:
            return JSONResponse(status_code=400, content={"status": "failed", "error": "memory_invalid_input"})

        from vibe.memory_http_headers import CALLER_SESSION_HEADER

        session_id = str(request.headers.get(CALLER_SESSION_HEADER) or "").strip()
        text = payload["text"]
        source_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

        try:
            implementation_error = getattr(controller, "_memory_implementation_error", None)
            if isinstance(
                implementation_error,
                (MemoryImplementationUnavailableError, MemoryImplementationIncompatibleError),
            ):
                raise implementation_error
            from avibe_memory import CaptureRequest

            capture = getattr(controller, "capture_memory", None)
            if not callable(capture):
                return JSONResponse(
                    status_code=503,
                    content={"status": "failed", "error": "memory_runtime_missing"},
                )
            receipt = await capture(
                CaptureRequest(
                    source_message_id=(
                        f"agent:{principal_id}:{project_id}:{session_id}:{source_digest}"
                    ),
                    session_id=session_id,
                    principal_id=principal_id,
                    project_id=project_id,
                    provenance="agent",
                    text=text,
                    occurred_at_ms=int(time.time() * 1000),
                )
            )
        except (MemoryImplementationUnavailableError, MemoryImplementationIncompatibleError) as exc:
            logger.warning("internal memory remember failed")
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": _memory_implementation_error_code(exc)},
            )
        except ImportError:
            implementation_error = getattr(controller, "_memory_implementation_error", None)
            if isinstance(
                implementation_error,
                (MemoryImplementationUnavailableError, MemoryImplementationIncompatibleError),
            ):
                return JSONResponse(
                    status_code=503,
                    content={
                        "status": "failed",
                        "error": _memory_implementation_error_code(implementation_error),
                    },
                )
            logger.warning("internal memory remember implementation unavailable")
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": "memory_implementation_unavailable"},
            )
        except Exception:
            implementation_error = getattr(controller, "_memory_implementation_error", None)
            if isinstance(
                implementation_error,
                (MemoryImplementationUnavailableError, MemoryImplementationIncompatibleError),
            ):
                return JSONResponse(
                    status_code=503,
                    content={
                        "status": "failed",
                        "error": _memory_implementation_error_code(implementation_error),
                    },
                )
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


    @app.post("/internal/model-hub")
    async def _model_hub(request: Request) -> Any:
        """Dispatch UI operations to the controller-owned Model Hub aggregate."""

        from config.v2_config import is_model_hub_enabled
        from core.handlers.model_hub import (
            ModelHubError,
            ensure_runtime_dependency,
            runtime_dependency_payload,
        )
        from core.handlers.model_hub.rpc import dispatch_model_hub_rpc

        body = await _safe_json(request)
        operation = body.get("operation") if isinstance(body, dict) else None
        payload = body.get("payload") if isinstance(body, dict) else None
        dependency_operation = operation == "runtime_ensure_dependency"
        if not is_model_hub_enabled() and not dependency_operation:
            return JSONResponse(
                status_code=404,
                content={"ok": False, "contract_version": 1, "error": "feature_disabled"},
            )
        if not isinstance(operation, str) or not isinstance(payload, dict):
            return JSONResponse(status_code=400, content={"ok": False, "error": "discovery_failed"})
        service = getattr(controller, "model_hub_service", None)
        adapter = getattr(controller, "model_hub_engine_adapter", None)
        if service is None and (not dependency_operation or adapter is None):
            return JSONResponse(status_code=503, content={"ok": False, "error": "engine_down"})
        try:
            if service is not None:
                result = await dispatch_model_hub_rpc(service, operation, payload)
            else:
                ensured = await ensure_runtime_dependency(
                    adapter,
                    force=payload.get("force") is True,
                    offline=payload.get("offline") is True,
                )
                result = runtime_dependency_payload(ensured, enabled=False)
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
        # Baselined before the handshake below, because that yield reaches the
        # bridge and a discard after it is a hole in what this feed promised.
        last_dropped = bus.dropped_count(sub_id)

        async def _stream():
            try:
                # A REAL ``connected`` event, not a ``:`` comment, which the
                # internal_client parser swallows: the bridge has to be able to
                # observe this handshake. It consumes the frame rather than
                # relaying it, and publishes the bridge-status transition that
                # browsers actually key off. Either way the UI reconciles after a
                # CONTROLLER restart that leaves the UI server + browser SSE up:
                # only this bridge reconnects, so the browser's own ``connected``
                # never fires and the crash-recovery ``running → idle`` reset
                # (broadcast to no subscriber) would otherwise stay invisible
                # until a manual reload (Codex P2).
                yield _sse_event("connected", {})
                while True:
                    # Same rule as the UI server's browser feed: a subscriber
                    # that lost an event is not a subscriber any more. Ending it
                    # makes the bridge reconnect, and a reconnect flips the
                    # bridge status, which is what tells browsers to reconcile.
                    # Announcing the hole down this stream instead cannot work --
                    # the queue is still full, so the next iteration finds
                    # another discard and announces again. Checked before
                    # ``get()`` so no event is relayed as if it followed the
                    # previous one when it does not.
                    dropped = bus.dropped_count(sub_id)
                    if dropped > last_dropped:
                        logger.warning(
                            "internal events: ending subscriber %s after %s dropped event(s)",
                            sub_id,
                            dropped - last_dropped,
                        )
                        return
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
    async def _cancel(session_id: str, run_id: str | None = None) -> Any:
        """HTTP adapter: delegate Stop to the turn owner (FSM, Phase 1b) and map its
        result ``code`` to a status — ``not_in_flight`` -> 404, ``stop_failed`` ->
        409. ``session_id`` is the dispatch key the turn registered under, so the UI
        Stop button works with just the URL it already has."""
        result = await manager.cancel(session_id, agent_run_id=run_id)
        code = result.get("code")
        if code == "not_in_flight":
            return JSONResponse(status_code=404, content=result)
        if code == "invalid_run_id":
            return JSONResponse(status_code=400, content=result)
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

    # Bind and report the canonical path so platform aliases (for example
    # macOS ``/var`` -> ``/private/var``) do not produce a mismatched endpoint.
    target = (socket_path or default_socket_path()).expanduser().resolve()
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

    payload: dict[str, str] = {
        "state": state,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if error is not None:
        payload["error"] = error
    if detail is not None:
        payload["detail"] = detail
    try:
        # Compact: the CLI polls this, nobody reads it by eye. Losing the write is
        # survivable — the status file is a courtesy to an out-of-process reader,
        # not something the server's own lifecycle depends on.
        write_atomic(
            paths.get_internal_server_status_path(),
            json.dumps(payload, separators=(",", ":")),
        )
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
        user_id=payload.get("author_id"),
        channel_id=payload.get("channel_id"),
        platform=payload.get("platform"),
        thread_id=payload.get("thread_id"),
        message_id=payload.get("message_id") or payload.get("user_message_id"),
        files=files,
    )
    if context.platform_specific is None:
        context.platform_specific = {}
    context.message_kind = normalize_message_kind(payload.get("message_kind"))
    context.is_original_human_text = context.message_kind == "original"
    context.platform_specific.update(
        {
            "delivery_id": payload.get("user_message_id"),
            "scope_id": payload.get("scope_id"),
            "display_text": payload.get("display_text"),
            "message_content": payload.get("content"),
            "message_metadata": payload.get("metadata") or {},
            "author_id": payload.get("author_id"),
            "author_name": payload.get("author_name"),
            "message_kind": context.message_kind,
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
    if not channel_id and is_dm and resolved_platform != "avibe":
        # A DM Session's scope_id is the USER id, which is not a channel. Every
        # other resolver (``running_agents._resolve_session_key_context``,
        # ``ScheduledTasks._resolve_target_context``) swaps in the bound
        # ``dm_chat_id``; this builder must too. Slack's ``chat.postMessage``
        # silently tolerates a user id (it opens the DM for you) so sending kept
        # working, but ``reactions.add`` rejects it with ``channel_not_found`` —
        # the reaction ack then failed and silently downgraded to an ack message.
        bound_dm_channel_id = _lookup_dm_channel_id(resolved_platform, str(resolved_user_id))
        if bound_dm_channel_id:
            resolved_channel_id = bound_dm_channel_id
    platform_specific: dict[str, Any] = {
        "agent_session_id": session_id,
        "platform": resolved_platform,
        "is_dm": is_dm,
    }
    if resolved_platform == "avibe":
        platform_specific["workbench_session_id"] = session_id
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
    )


def _lookup_dm_channel_id(platform: str, user_id: str) -> Optional[str]:
    """Return the bound DM channel id for ``user_id`` on ``platform``.

    Reads the persisted user scope settings directly (no controller / settings
    manager in scope here). Returns ``None`` when the user is unbound or has no
    recorded ``dm_chat_id`` — callers then keep the scope_id fallback.
    """

    if not platform or not user_id:
        return None
    try:
        from sqlalchemy import select

        from storage.models import scope_settings
        from storage.settings_service import make_scope_id

        scope_id = make_scope_id(platform, "user", user_id)
        engine = get_cached_sqlite_engine()
        with engine.connect() as conn:
            row = conn.execute(
                select(scope_settings.c.settings_json).where(scope_settings.c.scope_id == scope_id)
            ).first()
        if row is None or not row[0]:
            return None
        payload = json.loads(row[0])
    except Exception:
        logger.debug("internal_server: failed to resolve dm_chat_id for %s/%s", platform, user_id, exc_info=True)
        return None
    if not isinstance(payload, dict):
        return None
    return str(payload.get("dm_chat_id") or "").strip() or None


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
