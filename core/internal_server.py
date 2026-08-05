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
from vibe.message_types import types_with

if TYPE_CHECKING:  # pragma: no cover - typing only
    from core.controller import Controller

logger = logging.getLogger(__name__)
_SOCKET_MODE = 0o600
_SOCKET_UMASK_MODE = 0o700
_UNSUPPORTED_SOCKET_CHMOD_ERRNOS = frozenset(
    value
    for value in (
        errno.EINVAL,
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
    )
    if value is not None
)
_ACCEPTED_RESERVATION_TYPES = set(types_with("acceptedReservation"))
_MEMORY_LOG_CURSOR_RE = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
_MEMORY_LOG_ENTRY_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z")


def _memory_log_list_query(request: Request) -> tuple[str | None, int]:
    items = list(request.query_params.multi_items())
    keys = [key for key, _value in items]
    if any(key not in {"cursor", "limit"} for key in keys) or len(keys) != len(set(keys)):
        raise ValueError("invalid memory log query")
    values = dict(items)
    cursor = values.get("cursor")
    if cursor is not None and _MEMORY_LOG_CURSOR_RE.fullmatch(cursor) is None:
        raise ValueError("invalid memory log cursor")
    raw_limit = values.get("limit", "20")
    if not raw_limit.isascii() or not raw_limit.isdecimal():
        raise ValueError("invalid memory log limit")
    limit = int(raw_limit)
    if not 1 <= limit <= 50:
        raise ValueError("invalid memory log limit")
    return cursor, limit


def _memory_log_entry_query(request: Request) -> str:
    items = list(request.query_params.multi_items())
    if len(items) != 1 or items[0][0] != "memcell_id":
        raise ValueError("invalid memory log entry query")
    memcell_id = items[0][1]
    if _MEMORY_LOG_ENTRY_ID_RE.fullmatch(memcell_id) is None:
        raise ValueError("invalid memory log entry id")
    return memcell_id


def _create_controller_loop_server(config: Any) -> Any:
    """Create a uvicorn server without taking over process signal handlers."""

    import uvicorn

    class _ControllerLoopServer(uvicorn.Server):
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
    from core.session_turns import SessionTurnManager, queue_pending_user_message

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

    async def _submit_scheduled_turn(
        session_id: str,
        context: MessageContext,
        text: str,
        *,
        delivery_intent: str = "queue",
    ) -> Any:
        """Run a scheduled / watch turn through the SAME unified ``manager.submit``
        the interactive Chat path uses, so a scheduled run can never preempt an
        active Chat turn and gets the full turn lifecycle (in_flight + turn.start /
        turn.end + Stop) the Chat page renders (Codex P2). Unlike Chat there is no
        pre-persisted ``pending`` row to promote, so the enqueue callback ``append``s
        a fresh ``queued`` row attributed to the harness.
        """
        if not session_id:
            submission = await manager.submit(None, context, text, source=SOURCE_SCHEDULED)
            return submission.route

        native_message_id = str(getattr(context, "message_id", None) or "").strip()
        if native_message_id:
            active = manager.in_flight.get(session_id)
            active_message_id = str(getattr(getattr(active, "context", None), "message_id", None) or "").strip()
            if active_message_id == native_message_id:
                return "duplicate"
            from storage import messages_service

            engine = get_cached_sqlite_engine()
            with engine.connect() as conn:
                if messages_service.native_message_exists(
                    conn,
                    platform="avibe",
                    native_message_id=native_message_id,
                ):
                    return "duplicate"

        queue_owner_transferred = False
        queue_transfer_cancelled = False

        class _QueueTransferCancelled(RuntimeError):
            pass

        def _enqueue() -> bool:
            nonlocal queue_owner_transferred, queue_transfer_cancelled

            from core.message_mirror import _scope_id_for_session
            from core.session_turns import SCHEDULED_PROVENANCE_KEY, capture_scheduled_provenance
            from storage import messages_service
            from storage.background import (
                agent_run_cancellation_won_in_connection,
                hold_running_agent_run_for_workbench_in_connection,
                run_update_event_transaction,
            )

            # Persist the scheduled run's delivery / attribution provenance on the
            # queued row's metadata so flush_queue re-runs it as SOURCE_SCHEDULED with
            # that restored — keeping suppress_delivery / the delivery target / the task
            # attribution instead of degrading to a plain user turn (#84). The key's
            # PRESENCE also marks this row as a scheduled segment for the flush.
            engine = get_cached_sqlite_engine()
            with run_update_event_transaction(engine) as conn:
                scope_id = _scope_id_for_session(conn, session_id)
                try:
                    with conn.begin_nested():
                        messages_service.append(
                            conn,
                            scope_id=scope_id,
                            session_id=session_id,
                            platform="avibe",
                            author="harness",
                            source="harness",
                            message_type=messages_service.QUEUED_TYPE,
                            text=text,
                            metadata={SCHEDULED_PROVENANCE_KEY: capture_scheduled_provenance(context)},
                            native_message_id=native_message_id or None,
                        )
                        if delivery_intent == "send_now":
                            execution_id = str(
                                (context.platform_specific or {}).get(
                                    "task_execution_id"
                                )
                                or ""
                            ).strip()
                            if not hold_running_agent_run_for_workbench_in_connection(
                                conn,
                                execution_id,
                                delivery_outcome={
                                    "intent": "send_now",
                                    "status": "admitted",
                                    "target_was_busy": bool(
                                        manager.in_flight.get(session_id)
                                    ),
                                },
                            ):
                                if agent_run_cancellation_won_in_connection(
                                    conn,
                                    execution_id,
                                ):
                                    raise _QueueTransferCancelled
                                raise RuntimeError(
                                    "send-now Agent Run queue ownership transfer was refused"
                                )
                            queue_owner_transferred = True
                except _QueueTransferCancelled:
                    queue_transfer_cancelled = True
                    return False
                except IntegrityError:
                    logger.info("scheduled turn duplicate native id already queued: %s", native_message_id)
                    if delivery_intent == "send_now":
                        # A send-now attempt may not treat somebody else's
                        # duplicate row as its own admission; that would
                        # interrupt without transferring this Run.
                        return False
                    return bool(
                        native_message_id
                        and messages_service.native_message_exists(
                            conn,
                            platform="avibe",
                            native_message_id=native_message_id,
                        )
                    )
            return True

        submission = await manager.submit(
            session_id,
            context,
            text,
            source=SOURCE_SCHEDULED,
            enqueue=_enqueue,
            delivery_intent=delivery_intent,
        )
        if submission.route == "enqueued" and submission.queue_persisted is not True:
            if queue_transfer_cancelled:
                submission = replace(submission, delivery_status="canceled")
            else:
                raise RuntimeError("scheduled turn queue row was not persisted")
        if delivery_intent == "send_now":
            from storage.background import (
                record_agent_run_delivery_outcome_in_connection,
                run_update_event_transaction,
            )

            execution_id = str(
                (context.platform_specific or {}).get("task_execution_id") or ""
            ).strip()
            outcome = {
                "intent": "send_now",
                "status": submission.delivery_status or submission.route,
                "target_was_busy": submission.target_was_busy,
            }
            with run_update_event_transaction(get_cached_sqlite_engine()) as conn:
                if not record_agent_run_delivery_outcome_in_connection(
                    conn,
                    execution_id,
                    outcome,
                ):
                    raise RuntimeError(
                        "send-now Agent Run delivery outcome could not be recorded"
                    )
            submission = replace(
                submission,
                queue_owner_transferred=queue_owner_transferred,
            )
        # Existing scheduled/task/watch callers consume only the route string.
        # A direct Agent Run with send-now also needs the turn owner's exact
        # interrupt outcome, so return the structured result only for that
        # explicit contract and keep every legacy caller byte-compatible.
        return submission if delivery_intent == "send_now" else submission.route

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
        """Fire-and-forget turn dispatch for the session/page-scoped stream.

        Starts the turn and returns ``202`` immediately. The reply — plus any
        notify/result — reaches the browser over the persistent ``message.new``
        session stream, so the HTTP response isn't held open for the turn's
        duration and a closed browser tab can't cancel an in-flight turn.
        ``_run_turn`` holds the turn open (keeping ``in_flight`` populated so
        Stop works), publishes the turn lifecycle, and flushes the
        send-while-busy queue when it settles.
        """
        from storage import messages_service

        payload = await _safe_json(request)
        try:
            text, context = await _build_dispatch_payload(payload)
        except ValueError as err:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(err)})

        session_id = payload.get("session_id")
        sid = session_id if isinstance(session_id, str) and session_id else None
        user_message_id = payload.get("user_message_id")
        reserved_type: str | None = None

        def _reservation_state() -> tuple[dict[str, Any] | None, bool]:
            if not (
                isinstance(user_message_id, str)
                and user_message_id
                and sid
            ):
                return None, False

            from sqlalchemy import select
            from storage import workbench_sessions_service
            from storage.models import messages

            with get_cached_sqlite_engine().connect() as conn:
                reserved = conn.execute(
                    select(
                        messages.c.type,
                        messages.c.author,
                        messages.c.source,
                    ).where(
                        messages.c.id == user_message_id,
                        messages.c.session_id == sid,
                    )
                ).mappings().first()
                archived = workbench_sessions_service.is_session_archived(conn, sid)
            return reserved, archived

        async def _cancel_matching_turn() -> None:
            if not (
                isinstance(user_message_id, str)
                and user_message_id
                and sid
            ):
                return
            active = manager.in_flight.get(sid)
            active_message_id = str(
                getattr(getattr(active, "context", None), "message_id", None) or ""
            ).strip()
            if active_message_id == user_message_id:
                result = await manager.cancel(sid)
                if not result.get("ok") and not active.task.done():
                    active.task.cancel()
                await asyncio.gather(active.task, return_exceptions=True)
                if manager.in_flight.get(sid) is active:
                    manager.in_flight.pop(sid, None)
                    from core.inbox_events import bus

                    bus.publish("turn.end", {"session_id": sid})
                    controller.set_agent_status(sid, "idle")

        def _reservation_conflict(*, archived: bool) -> JSONResponse:
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "code": (
                        "session_archived"
                        if archived
                        else "message_reservation_lost"
                    ),
                    "session_id": session_id,
                    "message_id": user_message_id,
                },
            )

        def _enqueue() -> bool:
            # The caller already persisted an input as ``pending``; promote it to
            # ``queued`` so it drains after the active turn. Keep the exact
            # agent-facing text separately from its transcript display text.
            if isinstance(user_message_id, str) and user_message_id:
                engine = get_cached_sqlite_engine()
                with engine.begin() as conn:
                    return queue_pending_user_message(
                        conn,
                        user_message_id,
                        text,
                    )
            return False

        def _accept_reserved_input() -> dict[str, Any] | None:
            """Persist controller acceptance before the HTTP response can be lost."""

            if not (
                isinstance(user_message_id, str)
                and user_message_id
                and sid
            ):
                return None

            from core.inbox_events import bus
            from storage import messages_service

            promoted = False
            with get_cached_sqlite_engine().begin() as conn:
                row = messages_service.get_message(
                    conn,
                    user_message_id,
                    session_id=sid,
                )
                if row is None:
                    return None
                target_type = messages_service.pending_message_target_type(
                    row.get("author"),
                    row.get("source"),
                    row.get("author_name"),
                )
                promoted = messages_service.promote_pending(
                    conn,
                    user_message_id,
                    target_type,
                )
                if promoted:
                    row = messages_service.get_message(
                        conn,
                        user_message_id,
                        session_id=sid,
                    )

            if promoted and row is not None:
                bus.publish("message.new", row)
                bus.publish(
                    "session.activity",
                    {
                        "session_id": sid,
                        "scope_id": row.get("scope_id"),
                        "event": (
                            "show_event"
                            if row.get("author") == HARNESS_TYPE
                            and is_input_turn(
                                row.get("author"),
                                row.get("type"),
                            )
                            else "user_message"
                        ),
                    },
                )
                try:
                    with get_cached_sqlite_engine().connect() as conn:
                        inbox_row = messages_service.get_inbox_session(
                            conn,
                            sid,
                            platform="avibe",
                        )
                    if inbox_row is not None:
                        bus.publish("inbox.session.updated", inbox_row)
                except Exception:
                    logger.debug(
                        "inbox.session.updated publish (accepted input) failed",
                        exc_info=True,
                    )
            return row

        if isinstance(user_message_id, str) and user_message_id and sid:
            active = manager.in_flight.get(sid)
            active_message_id = str(
                getattr(getattr(active, "context", None), "message_id", None) or ""
            ).strip()
            reserved, archived = _reservation_state()
            if archived or reserved is None:
                return _reservation_conflict(archived=archived)
            reserved_type = str(reserved["type"])
            if (
                active_message_id == user_message_id
                and reserved_type == messages_service.PENDING_TYPE
            ):
                accepted = _accept_reserved_input()
                if accepted is None:
                    reserved, archived = _reservation_state()
                    if archived or reserved is None:
                        await _cancel_matching_turn()
                        return _reservation_conflict(archived=archived)
                reserved_type = (
                    str(accepted["type"])
                    if accepted is not None
                    else messages_service.PENDING_TYPE
                )
            if active_message_id == user_message_id or reserved_type in (
                _ACCEPTED_RESERVATION_TYPES - {messages_service.QUEUED_TYPE}
            ):
                return JSONResponse(
                    status_code=202,
                    content={
                        "ok": True,
                        "duplicate": True,
                        "session_id": session_id,
                        "message_id": user_message_id,
                        **({"message_type": reserved_type} if reserved_type else {}),
                    },
                )
            if reserved_type == messages_service.QUEUED_TYPE:
                return JSONResponse(
                    status_code=202,
                    content={
                        "ok": True,
                        "queued": True,
                        "duplicate": True,
                        "session_id": session_id,
                        "message_id": user_message_id,
                        "message_type": messages_service.QUEUED_TYPE,
                    },
                )

        submission = await manager.submit(sid, context, text, enqueue=_enqueue)
        settled_message_id = user_message_id
        settled_type = None
        if submission.route == "ran":
            settled = _accept_reserved_input()
            settled_type = settled.get("type") if settled is not None else None
        if (
            settled_type is None
            and isinstance(settled_message_id, str)
            and settled_message_id
            and sid
        ):
            settled, archived = _reservation_state()
            settled_type = str(settled["type"]) if settled is not None else None
            if archived or settled is None:
                if submission.route == "ran":
                    await _cancel_matching_turn()
                return _reservation_conflict(archived=archived)

        if submission.route == "enqueued":
            # An idle session can already have queue rows left by Stop. ``submit``
            # may drain synchronously before returning. Read the stored row rather
            # than inferring durability from the route.
            if settled_type != messages_service.QUEUED_TYPE:
                return JSONResponse(
                    status_code=202,
                    content={
                        "ok": True,
                        "drained": True,
                        "session_id": session_id,
                        **(
                            {"message_id": settled_message_id}
                            if settled_type
                            else {}
                        ),
                        **({"message_type": settled_type} if settled_type else {}),
                    },
                )
            return JSONResponse(
                status_code=202,
                content={
                    "ok": True,
                    "queued": True,
                    "session_id": session_id,
                    "message_id": settled_message_id,
                    "message_type": messages_service.QUEUED_TYPE,
                },
            )
        return JSONResponse(
            status_code=202,
            content={
                "ok": True,
                "session_id": session_id,
                **({"message_id": settled_message_id} if settled_type else {}),
                **({"message_type": settled_type} if settled_type else {}),
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

        from core.memory.artifact import MemoryRuntimeActivationError

        try:
            from config.v2_config import V2Config

            config = await asyncio.to_thread(V2Config.load)
            result = await controller.reconcile_memory(config.memory)
            return JSONResponse(status_code=200, content=result)
        except MemoryRuntimeActivationError:
            # Only the runtime install/activation bridge earns the "install
            # failed" message. Everything else reported it too, which sent an
            # incident caused by a pause/probe timeout in the wrong direction.
            logger.exception("internal memory runtime activation failed during reconcile")
            return JSONResponse(status_code=503, content={"ok": False, "error": "memory_runtime_install_failed"})
        except Exception:
            logger.exception("internal memory reconcile failed")
            return JSONResponse(status_code=503, content={"ok": False, "error": "memory_reconcile_failed"})

    @app.post("/internal/memory/restart")
    async def _memory_restart() -> Any:
        """Replace the live Memory sidecar through the Runtime lifecycle."""

        runtime = _memory_runtime()
        if runtime is None:
            return JSONResponse(
                status_code=503,
                content={"ok": False, "error": "memory_runtime_missing"},
            )
        try:
            result = await runtime.restart()
            return JSONResponse(status_code=200, content=result)
        except Exception:
            logger.exception("internal memory restart failed")
            return JSONResponse(
                status_code=503,
                content={"ok": False, "error": "memory_restart_failed"},
            )

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
            logger.exception("internal memory runtime install failed")
            return JSONResponse(status_code=503, content={"ok": False, "reason": "memory_runtime_install_failed"})

    def _memory_cli_scope(request: Request) -> tuple[str, str] | None:
        from core.memory.http_headers import CALLER_SESSION_HEADER

        session_id = str(request.headers.get(CALLER_SESSION_HEADER) or "").strip()
        if not session_id:
            return None
        resolve = getattr(controller, "memory_scope_for_cli_session", None)
        scope = resolve(session_id) if callable(resolve) else None
        from core.memory.store import is_principal_id, is_project_id

        if (
            isinstance(scope, tuple)
            and len(scope) == 2
            and is_principal_id(scope[0])
            and is_project_id(scope[1])
        ):
            return scope
        return None

    def _verified_memory_ui_user_key(request: Request) -> str | None:
        from core.memory.http_headers import (
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

    def _memory_read_scope(request: Request) -> tuple[str, str] | None:
        from core.memory.http_headers import MEMORY_USER_KEY_HEADER

        if str(request.headers.get(MEMORY_USER_KEY_HEADER) or "").strip():
            user_key = _verified_memory_ui_user_key(request)
            if user_key is None:
                return None
            runtime = _memory_runtime()
            try:
                principal_id = runtime.principal_for_user_key(user_key) if runtime is not None else None
                resolve_project = getattr(controller, "default_memory_project_id", None)
                project_id = resolve_project() if callable(resolve_project) else None
                from core.memory.store import is_principal_id, is_project_id

                if is_principal_id(principal_id) and is_project_id(project_id):
                    return principal_id, project_id
                return None
            except MemoryStoreUnavailableError:
                raise
            except Exception as exc:
                raise MemoryStoreUnavailableError("Memory store is unavailable") from exc
        return _memory_cli_scope(request)

    memory_admin_log_access = object()

    def _memory_log_access(request: Request) -> object | tuple[str, str] | None:
        from core.memory.http_headers import MEMORY_USER_KEY_HEADER

        if str(request.headers.get(MEMORY_USER_KEY_HEADER) or "").strip():
            return (
                memory_admin_log_access
                if _verified_memory_ui_user_key(request) is not None
                else None
            )
        return _memory_cli_scope(request)

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
            scope = _memory_read_scope(request)
        except MemoryStoreUnavailableError:
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": "memory_store_unavailable"},
            )
        if scope is None:
            return JSONResponse(status_code=403, content={"status": "failed", "error": "memory_access_denied"})
        principal_id, project_id = scope
        runtime = _memory_runtime()
        if runtime is None:
            return JSONResponse(status_code=503, content={"status": "failed", "error": "memory_runtime_missing"})
        try:
            return await runtime.profile_payload(principal_id, project_id)
        except Exception:
            logger.warning("internal memory profile failed")
            return JSONResponse(status_code=503, content={"status": "failed", "error": "memory_processing_failed"})

    @app.get("/internal/memory/log")
    async def _memory_log(request: Request) -> Any:
        try:
            cursor, limit = _memory_log_list_query(request)
            access = _memory_log_access(request)
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
        if access is None:
            return JSONResponse(
                status_code=403,
                content={"status": "failed", "error": "memory_access_denied"},
            )
        runtime = _memory_runtime()
        if runtime is None:
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": "memory_runtime_missing"},
            )
        try:
            if access is memory_admin_log_access:
                return await runtime.admin_log_entries_payload(cursor, limit)
            principal_id, project_id = access
            return await runtime.log_entries_payload(
                principal_id,
                project_id,
                cursor,
                limit,
            )
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"status": "failed", "error": "memory_invalid_input"},
            )
        except Exception:
            logger.warning("internal memory log failed")
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": "memory_processing_failed"},
            )

    @app.get("/internal/memory/log/entry")
    async def _memory_log_entry(request: Request) -> Any:
        try:
            memcell_id = _memory_log_entry_query(request)
            access = _memory_log_access(request)
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
        if access is None:
            return JSONResponse(
                status_code=403,
                content={"status": "failed", "error": "memory_access_denied"},
            )
        runtime = _memory_runtime()
        if runtime is None:
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": "memory_runtime_missing"},
            )
        try:
            if access is memory_admin_log_access:
                payload = await runtime.admin_log_entry_payload(memcell_id)
            else:
                principal_id, project_id = access
                payload = await runtime.log_entry_payload(
                    principal_id,
                    project_id,
                    memcell_id,
                )
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"status": "failed", "error": "memory_invalid_input"},
            )
        except Exception:
            logger.warning("internal memory log entry failed")
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": "memory_processing_failed"},
            )
        if payload.get("status") == "not_found":
            return JSONResponse(
                status_code=404,
                content={"status": "failed", "error": "memory_log_entry_not_found"},
            )
        return payload

    @app.post("/internal/memory/search")
    async def _memory_search(request: Request) -> Any:
        try:
            scope = _memory_read_scope(request)
        except MemoryStoreUnavailableError:
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": "memory_store_unavailable"},
            )
        if scope is None:
            return JSONResponse(status_code=403, content={"status": "failed", "error": "memory_access_denied"})
        principal_id, project_id = scope
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
            return await runtime.search_payload(payload["query"], limit, principal_id, project_id)
        except Exception:
            logger.warning("internal memory search failed")
            return JSONResponse(status_code=503, content={"status": "failed", "error": "memory_processing_failed"})

    @app.post("/internal/memory/remember")
    async def _memory_remember(request: Request) -> Any:
        scope = _memory_cli_scope(request)
        if scope is None:
            return JSONResponse(status_code=403, content={"status": "failed", "error": "memory_access_denied"})
        principal_id, project_id = scope
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
        if code == "stop_failed":
            return JSONResponse(status_code=409, content=result)
        return result

    @app.post("/internal/send-now/{session_id}")
    async def _send_now(session_id: str) -> Any:
        """HTTP adapter: delegate "立即发送" (run the send-while-busy queue now) to
        the turn owner (FSM, Phase 1b); typed failures remain HTTP failures."""
        result = await manager.send_now(session_id)
        code = result.get("code")
        if code == "stop_failed":
            return JSONResponse(status_code=409, content=result)
        if code == "flush_failed":
            return JSONResponse(status_code=503, content=result)
        return result

    # Expose the per-session turn gate to in-process callers (the scheduler)
    # WITHOUT going through the HTTP surface: ``ScheduledTaskService`` runs on the
    # same loop and routes avibe scheduled / watch turns through
    # ``submit_scheduled`` so they share the Chat path's queueing + lifecycle.
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
    manager = getattr(controller, "session_turns", None)
    recover_queue = getattr(manager, "recover_persisted_agent_run_queue", None)
    if callable(recover_queue):
        try:
            recovered = await recover_queue()
            if recovered:
                logger.info(
                    "Recovered persisted Workbench Agent Run queues for %s",
                    ",".join(recovered),
                )
        except Exception:
            logger.exception("Failed to recover persisted Workbench Agent Run queues")
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
    """Build the avibe ``MessageContext`` for a workbench session.

    Shared by the dispatch endpoint and the cancel endpoint so a stop reuses
    the exact same session-routing context (chosen agent / model / effort,
    native session id, workdir) the turn ran under — that's what lets cancel
    reuse the IM ``/stop`` path to interrupt the right backend session.
    Defaults to ``platform="avibe"``.
    """

    # ``agent_session_id`` is the agent_sessions PK; persist_agent_message reads
    # it to attribute avibe agent replies to the right session (IM stamps it at
    # session-resolve time). For avibe the dispatch session_id IS that PK.
    platform_specific: dict[str, Any] = {
        "workbench_session_id": session_id,
        "agent_session_id": session_id,
    }
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
        user_id=str(user_id or "workbench"),
        channel_id=str(channel_id or session_id),
        platform=platform or "avibe",
        thread_id=thread_id,
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
