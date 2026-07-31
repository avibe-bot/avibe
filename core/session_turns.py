"""Per-session turn ownership for the avibe workbench.

Phase 1b of the turn-lifecycle FSM (``docs/plans/avibe-turn-lifecycle-fsm.md``):
introduce ONE owner of a session's turn state so the gate, dispatcher, scheduler,
and restore paths stop reconciling several separate stores. A session has **at
most one active turn** (IDLE ↔ RUNNING; no turn-duration timeout — a long agent
runs until it emits its terminal result or the user Stops it).

``SessionTurnManager`` is wired as ``controller.session_turns`` by
``core.internal_server.create_app``. It owns the in_flight registry + the
flush-intent sets, and the turn lifecycle: ``submit`` (start + hold-open) and
``flush_queue`` (drain the send-while-busy queue). The internal-server HTTP
handlers and the scheduler are thin callers. Cancel / send-now / turn-state /
terminal-result move onto the manager in subsequent commits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Literal, Optional

from sqlalchemy import select, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from core.web_push_notifications import WEB_PUSH_USER_KEY_METADATA, WEB_PUSH_USER_KEYS_METADATA
from core.run_settlement import (
    SETTLEMENTS_WITHOUT_RESULT,
    SETTLED_BY_BACKEND_REFRESH,
    SETTLED_BY_NO_TERMINAL_RESULT,
    SETTLED_BY_REFUSED_CONCURRENT_TURN,
    SETTLED_BY_STOPPED,
    SETTLED_BY_TERMINAL_RESULT,
)
from core.services.dispatch import SOURCE_HUMAN, SOURCE_SCHEDULED, dispatch_turn_with_outcome
from core.services.agent_steering import (
    SteerOutcome,
    SteerRequest,
    active_steer_identity,
    result as steer_result,
    steer_active_turn,
)
from storage import messages_service
from storage import message_deliveries as delivery_store
from storage.agent_session_rows import reserve_write_lock
from storage.db import get_cached_sqlite_engine
from storage.background import normalize_run_status
from storage.models import agent_runs, agent_sessions, session_turns as session_turn_rows
from storage.workbench_sessions_service import derive_session_harness_activities
from core.message_output import terminal_turn_output
from vibe.i18n import t as i18n_t

if TYPE_CHECKING:
    from modules.im import MessageContext

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_backend_activity_item(item: dict[str, Any]) -> dict[str, Any]:
    """Annotate a registry background activity with the unified banner fields.

    The registry (``core/session_activities.py``) is left untouched; we only tag
    its serialized output so a backend activity and a live-derived harness item
    share one shape (``item_kind`` / ``label`` / ``since``) for the banner.
    """
    enriched = dict(item)
    enriched["item_kind"] = "backend_activity"
    enriched["since"] = str(item.get("started_at") or "")
    enriched["label"] = str(item.get("description") or "")
    return enriched


# A queued row's ``metadata[SCHEDULED_PROVENANCE_KEY]`` carries the scheduled run's
# context.platform_specific provenance that the gate must restore when the row is
# finally flushed — so a scheduled run enqueued behind an active turn keeps its
# delivery override / suppression / task attribution + runs as SOURCE_SCHEDULED, not
# a plain human turn (#84). Its PRESENCE also marks the row as a scheduled segment
# (vs a user send) for flush_queue.
SCHEDULED_PROVENANCE_KEY = "scheduled_provenance"
SCHEDULED_QUEUE_MERGE_WINDOW_SECONDS = 60
SCHEDULED_QUEUE_BURST_HINT_THRESHOLD = 3
SCHEDULED_QUEUE_FULL_DETAIL_LIMIT = 3
_SHOW_CHECKPOINT_DEFERRED_START_KEY = "_avibe_show_git_deferred_start"
_SHOW_CHECKPOINT_TERMINAL_PENDING_KEY = "_avibe_show_git_terminal_pending"

# The platform_specific keys the FLUSH rebuilds fresh from the session row (avibe
# routing). Everything ELSE the scheduled context carries is delivery / attribution
# provenance to preserve. We capture by EXCLUDING these (a blocklist) rather than
# whitelisting provenance keys, so a delivery field like ``delivery_override`` — what
# ``MessageDispatcher._get_target_context`` actually redirects delivery on — can't be
# silently omitted (Codex P1 #3338692433).
_FLUSH_REBUILT_KEYS = frozenset(
    {"platform", "is_dm", "workbench_session_id", "agent_session_id", "agent_session_target", "turn_token"}
)
SCHEDULED_TARGET_AGENT_KEY = "scheduled_target_agent_name"


def capture_scheduled_provenance(context: "MessageContext") -> dict:
    """Capture the scheduled run's provenance to persist on its queued row so
    flush_queue can restore it (#84):

    - ``message_id`` — the top-level stable ``scheduled:/watch:/webhook:`` native id
      that ``mirror_harness_inbound`` persists the prompt under, and that the
      ``(platform, native_message_id)`` uniqueness dedupes a retried/duplicated
      execution on. The flush's rebuilt context is otherwise ``message_id=None`` so a
      queued retry would lose dedup + native provenance (Codex P2 #3338722672).
    - ``platform_specific`` — the delivery / attribution slice: everything EXCEPT the
      routing keys the flush rebuilds, captured by exclusion so a delivery field like
      ``delivery_override`` can't be silently missed (Codex P1 #3338692433).
    """
    spec = getattr(context, "platform_specific", None) or {}
    captured_spec = {k: v for k, v in spec.items() if k not in _FLUSH_REBUILT_KEYS}
    target = spec.get("agent_session_target")
    if isinstance(target, dict):
        target_agent = str(target.get("agent_name") or "").strip()
        if target_agent:
            captured_spec.setdefault(SCHEDULED_TARGET_AGENT_KEY, target_agent)
    return {
        "message_id": getattr(context, "message_id", None),
        "platform_specific": captured_spec,
    }


def _run_metadata_holds_delivery(value: Any) -> bool:
    try:
        metadata = json.loads(value or "{}")
    except (TypeError, ValueError):
        return False
    return bool(
        isinstance(metadata, dict)
        and metadata.get("workbench_queue_holds_run") is True
    )


def emit_matches_active_turn(sink: dict, context: "MessageContext") -> bool:
    """The ONE active-turn token rule (FSM Phase 2 — collapses the three previously
    duplicated guards: ``_stream_chunk`` completion, ``_is_active_turn``, and
    ``Controller.mark_turn_complete``).

    A live sink WITH a token means an interactive turn is in flight; only its OWN
    result (matching token) is the active turn's. A result whose token DIFFERS or is
    ABSENT is stale — a superseded / stopped / older turn, or a scheduled / watch run
    that carries no token — and must NOT complete the turn (set ``done_event``) or
    settle its dot. Fail-open when the sink itself is tokenless, so non-streaming
    turns still settle. (Chunk FORWARDING is deliberately NOT gated — see
    ``_stream_chunk``; only COMPLETION + dot-settle are.)

    NOTE (no-timeout invariant): with the turn-duration timeout gone, a turn whose
    OWN terminal result is tokenless would hang here forever. The FSM therefore must
    guarantee every terminal result carries the active turn's token (Claude adoption
    / FSM-attached token); this guard is intentionally strict.
    """
    sink_token = sink.get("turn_token")
    ctx_token = (getattr(context, "platform_specific", None) or {}).get("turn_token")
    return not (sink_token is not None and ctx_token != sink_token)


@dataclass
class Turn:
    """The one active turn for an avibe session — the EXECUTION half of the FSM
    state, keyed by ``session_id`` in ``SessionTurnManager.in_flight``.

    - ``task`` / ``context``: the running dispatch task + the ``MessageContext`` the
      turn STARTED under (so Stop interrupts the backend it actually ran on, even if
      the Chat header later swapped agent/model). ``task`` is the Stop target
      (``/internal/cancel``) and the ``/turn-state`` source.
    - ``flush_on_cancel``: drain the send-while-busy queue even though the turn ends
      via cancellation — ``send-now`` cancels the running turn but wants the queue to
      run right after. A plain Stop keeps the queue ("不清空队列").
    - ``stop_no_flush``: a plain Stop is interrupting this turn and it must NOT flush,
      even if the backend interrupt lets the turn settle normally (no
      ``CancelledError``) during the awaited stop.

    The two intents live HERE rather than in parallel ``set``s so they retire with
    the turn: ``cancel`` / ``send_now`` set them on this object and ``_run`` reads
    them off the SAME object when it pops it — no separate ``.discard()`` to leak.

    The streaming SINK is deliberately NOT held here: it is keyed by ``session_key``
    (platform-prefixed ``avibe::<id>``) not ``session_id``, is registered from the
    dispatcher on the emit path, and is platform-agnostic (a future IM stream has a
    sink but no avibe ``session_id``). See ``SessionTurnManager.active_turn_sinks``.
    """

    task: asyncio.Task
    context: "MessageContext"
    started_at: str = ""
    flush_on_cancel: bool = False
    stop_no_flush: bool = False
    #: WHY this turn's task was cancelled, in the ``core.run_settlement``
    #: vocabulary — set by the canceller BEFORE ``task.cancel()`` so ``_run`` can
    #: attribute the run it owns correctly. User interruption paths set
    #: ``SETTLED_BY_STOPPED`` before invoking the backend because a successful Stop
    #: may emit its terminal result before ``handle_stop`` returns; backend runtime
    #: refresh sets ``SETTLED_BY_BACKEND_REFRESH`` so a routine ``agents.*``
    #: reconciliation is not reported as if the user pressed Stop (Codex P1). It
    #: rides on the Turn for the same reason the flush intents do: it retires when
    #: the turn is popped, with no parallel set to leak.
    cancel_settled_by: Optional[str] = None
    #: One shared backend Stop attempt for concurrent send-now callers. Each
    #: caller already owns its own durable queue row; they must not interrupt the
    #: same active turn more than once.
    send_now_task: Optional[asyncio.Task[dict[str, Any]]] = None
    logical_turn_id: Optional[str] = None
    delivery_id: Optional[str] = None
    terminal_is_error: bool = False


@dataclass(frozen=True)
class DeliveryRequest:
    """Private P0/P1/P3 admission contract owned by ``SessionTurnManager``."""

    session_id: str
    priority: Literal["p0", "p1", "p3"]
    content: str | None = None
    # Empty dispatch text can still carry accepted communication content through
    # attachments. ``None`` preserves the public empty-P0/P1 control semantics.
    has_content: bool | None = None
    delivery_id: str | None = None
    scope_id: str | None = None
    platform: str = "avibe"
    source: str = "user"
    author: str = "user"
    message_type: str | None = None
    author_id: str | None = None
    author_name: str | None = None
    display_text: str | None = None
    content_json: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    native_message_id: str | None = None
    parent_native_message_id: str | None = None


@dataclass(frozen=True)
class DeliveryResult:
    delivery_id: str | None
    message_id: str | None
    state: str
    turn_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class TurnSubmissionResult:
    """Routing decision plus the durable queue / delivery-intent outcome."""

    route: Literal["ran", "enqueued"]
    queue_persisted: bool | None = None
    target_was_busy: bool = False
    delivery_status: str | None = None
    queue_owner_transferred: bool = False


class SessionTurnManager:
    """Owns the live per-session turn state + lifecycle for avibe sessions.

    State (a session has at most one active turn):

    - ``in_flight``: ``session_id -> Turn`` for the active turn — the Stop target
      (``/internal/cancel``), the ``/turn-state`` source, the trigger for draining
      the send-while-busy queue, and the carrier of the two end-of-turn flush
      intents (``Turn.flush_on_cancel`` / ``Turn.stop_no_flush``).
    - ``active_turn_sinks``: the live streaming sink per ``session_key`` — the
      streaming half, kept separate on purpose (see ``Turn``).

    ``controller`` reaches the backends + the outbound chokepoint
    (``emit_agent_message``); ``build_context`` rebuilds a session's routing
    ``MessageContext`` for a queued follow-up (injected by the gate because it
    lives in ``internal_server``).
    """

    def __init__(
        self,
        controller: Any = None,
        *,
        build_context: Optional[Callable[[str], "MessageContext"]] = None,
    ) -> None:
        self.controller = controller
        self._build_context = build_context
        self._engine: Engine | None = None
        self.in_flight: dict[str, Turn] = {}
        self._draining_backends: set[str] = set()
        self._deferred_restart_sessions: dict[str, set[str]] = {}
        self._queue_recovery_locks: dict[str, asyncio.Lock] = {}
        # The live streaming turn sink per SESSION KEY (avibe/web-Chat only; IM/CLI
        # turns register none). Each is ``{on_chunk, done_event, turn_token}`` — the
        # turn's stream callback + completion event + correlation token. Keyed by
        # session_key (stable across a session's turns) so a reused agent receiver
        # carrying a stale per-turn context still resolves the current turn's sink.
        self.active_turn_sinks: dict[str, dict] = {}

    def _sqlite_engine(self) -> Engine:
        if self._engine is None:
            self._engine = get_cached_sqlite_engine()
        return self._engine

    def _durable_schema_available(self) -> bool:
        engine = self._sqlite_engine()
        with engine.connect() as conn:
            return conn.dialect.has_table(
                conn,
                "session_turns",
            ) and conn.dialect.has_table(
                conn,
                "message_deliveries",
            ) and conn.dialect.has_table(conn, "agent_sessions")

    def is_in_flight(self, session_id: Optional[str]) -> bool:
        """True when ``session_id`` has an active (RUNNING) turn."""
        return bool(session_id) and session_id in self.in_flight

    @staticmethod
    def _agent_run_ids_from_spec(spec: Any) -> set[str]:
        """Every ``agent_runs`` id a turn started under this spec is settling."""
        if not isinstance(spec, dict):
            return set()
        found: set[str] = set()
        primary = str(spec.get("task_execution_id") or "").strip()
        if primary:
            found.add(primary)
        coalesced = spec.get("coalesced_queue")
        execution_ids = coalesced.get("execution_ids") if isinstance(coalesced, dict) else None
        if isinstance(execution_ids, list):
            for value in execution_ids:
                execution_id = str(value or "").strip()
                if execution_id:
                    found.add(execution_id)
        return found

    def _settle_turn_owned_agent_runs(self, context: "MessageContext", settled_by: Optional[str]) -> None:
        """Settle the ``agent_runs`` rows this turn owned when no result arrived.

        The turn lane needs its own settlement because the harness cannot do it:
        ``_execute_agent_run`` hands an avibe-targeted run to
        ``session_turn_gate.submit_scheduled`` and returns with
        ``complete_on_return=False`` while the turn is still running, so the outcome
        this turn eventually produces is never seen there (Codex P1). Without this, a
        stopped Workbench run whose backend emits no terminal result stays ``running``
        until the staleness sweep relabels it ``orphaned`` — or forever when the sweep
        is disabled.

        Only a settlement that means "no result is coming" may terminalize the row —
        membership in ``SETTLEMENTS_WITHOUT_RESULT``, not "anything but
        ``terminal_result``". A real terminal output that completes the turn while
        leaving the run to another owner (``turn_only_result``: the requeued Activity
        behind a Claude delivery failure) must be left alone, or its run is failed and
        its callback fired before the retry runs (Codex P1). ``None`` means no sink
        existed, so there is nothing to conclude — do not guess. A coalesced turn settles EVERY id it owns, matching
        ``owned_agent_run_ids``. A plain Chat turn carries no run attribution and no-ops
        here. The write itself is the guarded first-writer-wins one, so racing an honest
        terminal result is safe in both directions.
        """

        if settled_by not in SETTLEMENTS_WITHOUT_RESULT:
            return
        run_ids = self._agent_run_ids_from_spec(getattr(context, "platform_specific", None))
        if not run_ids:
            return
        service = getattr(self.controller, "scheduled_task_service", None) if self.controller else None
        settle = getattr(service, "settle_agent_runs_without_result", None)
        if not callable(settle):
            # A fake/partial controller (tests, a boot window before the service
            # exists). The sweep remains the backstop; guessing is not an option.
            logger.debug("turn settlement: no harness settlement writer available")
            return
        try:
            settle(sorted(run_ids), settled_by=settled_by)
        except Exception:
            logger.warning(
                "turn settlement: failed to settle runs %s as %s",
                sorted(run_ids),
                settled_by,
                exc_info=True,
            )

    def owned_agent_run_ids(self) -> set[str]:
        """Run ids a live turn in THIS process is currently executing.

        The staleness sweep (``docs/plans/agent-run-zombie-settlement.md`` §4.1) needs
        to know which ``running`` rows still have an owner before it declares any of
        them orphaned. ``ScheduledTaskService`` can answer for its own drain path, but
        the workbench lane never enters ``_inflight_executions``:
        ``claim_queued_runs_for_workbench_in_connection`` claims rows that
        ``flush_queue`` executes. ``Turn`` carries no run-id field, so the ids are read
        off the context each turn started under — the same ``task_execution_id`` /
        ``coalesced_queue`` keys ``_turn_sink_identity`` reads.

        A coalesced turn owns EVERY id it is settling, not just the primary, or the
        sweep would fail the siblings out from under a live flush.

        Live streaming sinks are unioned in as well. They carry the same attribution
        and are registered/popped on a different boundary than ``in_flight``, so a run
        visible through either one is owned. Over-reporting an owner only delays a
        sweep; under-reporting one fails a live run.

        A malformed context contributes no ids rather than raising — but note the
        caller must still fail closed if this method is missing or raises outright,
        because "no owners" and "cannot tell" are opposite answers.
        """
        owned: set[str] = set()
        for turn in list(self.in_flight.values()):
            owned |= self._agent_run_ids_from_spec(getattr(turn.context, "platform_specific", None))
        for sink in list(self.active_turn_sinks.values()):
            owned |= self._agent_run_ids_from_spec(sink)
        return owned

    def busy_session_ids(self) -> set[str]:
        """Sessions whose gate is occupied by a live turn RIGHT NOW.

        This is the exemption the staleness sweep needs for the ``queue_hold_expired``
        class, and it is deliberately NOT expressible through
        :meth:`owned_agent_run_ids`: a run the gate parked behind a live turn is not one
        the live turn is executing, so it has no owner to report it, yet it is not
        abandoned either — ``flush_queue`` will pick it up when the turn ends. Without
        this, a legitimate Workbench turn lasting longer than the hold TTL had its own
        queued follower failed underneath it (Codex P2).

        ``in_flight`` covers live tasks in this process. A restored native runtime can
        instead be owned only by ``session_turns``; ``submit`` treats that durable owner
        as busy too, so the sweep projection must use the same combined predicate.
        Sinks are not unioned in here: they are keyed by session KEY, not session id,
        and a streaming turn always has one of those two owners.
        """
        busy = {session_id for session_id in list(self.in_flight) if session_id}
        if self._durable_schema_available():
            with self._sqlite_engine().connect() as conn:
                busy |= delivery_store.session_ids_with_live_turns(conn)
        return busy

    def model_hub_turn_id_for_task(
        self,
        task: Optional[asyncio.Task] = None,
    ) -> Optional[str]:
        """Return the existing FSM turn token owned by ``task``.

        Model Hub launch resolution runs inside the turn's dispatch task. IM and
        CLI calls do not enter this registry, so they intentionally return no id.
        """

        owner = task or asyncio.current_task()
        if owner is None:
            return None
        for turn in list(self.in_flight.values()):
            if turn.task is not owner or turn.task.done():
                continue
            token = str(
                (getattr(turn.context, "platform_specific", None) or {}).get(
                    "turn_token"
                )
                or ""
            ).strip()
            return token or None
        return None

    def _settle_model_hub_turn(
        self,
        context: "MessageContext",
        settled_by: Optional[str],
    ) -> None:
        runtime = getattr(self.controller, "model_hub_runtime", None)
        settle = getattr(runtime, "settle_turn", None)
        turn_id = str(
            (getattr(context, "platform_specific", None) or {}).get("turn_token")
            or ""
        ).strip()
        if not turn_id or not callable(settle):
            return
        try:
            from modules.agents.model_hub import (
                launch_for_context,
                turn_mode_for_context,
            )

            launch = launch_for_context(context)
            mode = turn_mode_for_context(context)
            if mode is None and launch is not None:
                mode = "direct" if launch.channel == "direct" else "hub"
            settle(
                turn_id,
                settled_by=settled_by,
                ts=_utc_now_iso(),
                mode=mode,
            )
        except Exception:
            logger.warning(
                "Model Hub provenance settlement failed for turn=%s",
                turn_id,
                exc_info=True,
            )

    def bind_context(self, build_context: Callable[[str], "MessageContext"]) -> None:
        """Inject the routing-context builder (it lives in ``internal_server``) once
        the gate is built, so ``flush_queue`` can rebuild a queued follow-up's
        routing from the current session row."""
        self._build_context = build_context

    def _context_backend(self, context: "MessageContext") -> str:
        spec = getattr(context, "platform_specific", None) or {}
        target = spec.get("agent_session_target")
        backend = str(target.get("agent_backend") or "").strip() if isinstance(target, dict) else ""
        if backend:
            return backend
        resolved = spec.get("resolved_vibe_agent")
        if isinstance(resolved, dict):
            backend = str(resolved.get("backend") or "").strip()
            if backend:
                return backend
        resolver = getattr(self.controller, "resolve_agent_for_context", None)
        if callable(resolver):
            try:
                return str(resolver(context) or "").strip()
            except Exception:
                logger.debug("Failed to resolve inherited backend for restart drain", exc_info=True)
        service = getattr(self.controller, "agent_service", None)
        return str(getattr(service, "default_agent", "") or "").strip()

    def begin_backend_drain(self, backend: str) -> None:
        self._draining_backends.add(backend)
        self._deferred_restart_sessions.setdefault(backend, set())

    async def end_backend_drain(self, backend: str, *, resume_deferred: bool = True) -> None:
        self._draining_backends.discard(backend)
        session_ids = self._deferred_restart_sessions.pop(backend, set())
        if not resume_deferred:
            return
        for session_id in sorted(session_ids):
            if not self.is_in_flight(session_id):
                if self._durable_schema_available():
                    await self._resume_post_terminal(session_id)
                else:
                    await self.flush_queue(session_id)

    def active_session_ids_for_backend(self, backend: str) -> set[str]:
        return {
            session_id
            for session_id, turn in self.in_flight.items()
            if not turn.task.done() and self._context_backend(turn.context) == backend
        }

    def active_runtime_session_ids_for_backend(self, backend: str) -> set[str]:
        """Active Sessions that actually entered the old backend generation."""
        active = {
            session_id
            for session_id, turn in self.in_flight.items()
            if not turn.task.done()
            and self._context_backend(turn.context) == backend
            and bool(
                (getattr(turn.context, "platform_specific", None) or {}).get(
                    "agent_runtime_turn_token"
                )
            )
        }
        if self._durable_schema_available():
            with self._sqlite_engine().connect() as conn:
                active |= delivery_store.active_runtime_session_ids_for_backend(
                    conn,
                    backend,
                )
        return active

    @staticmethod
    async def _noop_chunk(_envelope: dict) -> None:
        # Chunks are discarded — the browser renders from ``message.new``.
        return None

    def _delivery_context(self, session_id: str) -> "MessageContext":
        if self._build_context is None:
            raise RuntimeError("Session delivery context builder is not bound")
        return self._build_context(session_id)

    def _delivery_backend(self, session_id: str, context: Optional["MessageContext"]) -> tuple[str, "MessageContext"]:
        resolved = context or self._delivery_context(session_id)
        with self._sqlite_engine().connect() as conn:
            backend = str(
                conn.execute(
                    select(agent_sessions.c.agent_backend).where(
                        agent_sessions.c.id == session_id
                    )
                ).scalar_one_or_none()
                or ""
            ).strip()
        if not backend:
            backend = self._context_backend(resolved)
        if not backend:
            raise RuntimeError(f"Session {session_id} has no resolved backend")
        return backend, resolved

    @staticmethod
    def _delivery_snapshot(request: DeliveryRequest) -> dict[str, Any]:
        if request.content is None:
            raise ValueError("content Delivery requires a Message snapshot")
        return delivery_store.message_snapshot(
            scope_id=request.scope_id,
            session_id=request.session_id,
            platform=request.platform,
            author=request.author,
            source=request.source,
            message_type=request.message_type,
            text=request.display_text if request.display_text is not None else request.content,
            content=request.content_json,
            metadata=request.metadata,
            author_id=request.author_id,
            author_name=request.author_name,
            native_message_id=request.native_message_id,
            parent_native_message_id=request.parent_native_message_id,
        )

    def _request_from_delivery(self, delivery: dict[str, Any]) -> DeliveryRequest:
        payload = delivery_store.delivery_payload(delivery)
        return DeliveryRequest(
            session_id=str(delivery["session_id"]),
            priority=str(delivery["priority"]),
            content=str(delivery.get("dispatch_text") or ""),
            has_content=True,
            delivery_id=str(delivery["id"]),
            scope_id=payload.get("scope_id"),
            platform=str(payload.get("platform") or "avibe"),
            source=str(payload.get("source") or "user"),
            author=str(payload.get("author") or "user"),
            message_type=str(payload.get("type") or "user"),
            author_id=payload.get("author_id"),
            author_name=payload.get("author_name"),
            display_text=str(payload.get("text") or ""),
            content_json=dict(payload.get("content") or {}),
            metadata=dict(payload.get("metadata") or {}),
            native_message_id=payload.get("native_message_id"),
            parent_native_message_id=payload.get("parent_native_message_id"),
        )

    def _hydrate_delivery_context(
        self,
        context: "MessageContext",
        delivery: dict[str, Any],
    ) -> dict[str, Any]:
        """Restore dispatch inputs only from the durable Delivery snapshot."""

        payload = delivery_store.delivery_payload(delivery)
        context.message_id = str(delivery["id"])
        context.platform = str(payload.get("platform") or context.platform or "avibe")
        if context.platform_specific is None:
            context.platform_specific = {}
        context.platform_specific.update(
            {
                "delivery_id": str(delivery["id"]),
                "scope_id": payload.get("scope_id"),
                "display_text": payload.get("text") or "",
                "message_content": dict(payload.get("content") or {}),
                "message_metadata": dict(payload.get("metadata") or {}),
                "author_id": payload.get("author_id"),
                "author_name": payload.get("author_name"),
                "native_message_id": payload.get("native_message_id"),
            }
        )
        from core.workbench_media import (
            file_attachments_from_specs,
            resolve_attachment_specs,
        )

        attachments = (payload.get("content") or {}).get("attachments") or []
        with self._sqlite_engine().connect() as conn:
            specs = resolve_attachment_specs(
                conn,
                session_id=str(delivery["session_id"]),
                attachments=attachments,
            )
        context.files = file_attachments_from_specs(specs)
        return payload

    @staticmethod
    def _insert_delivery(
        conn: Connection,
        request: DeliveryRequest,
        *,
        priority: str,
        state: str,
    ) -> dict[str, Any]:
        delivery_id = request.delivery_id or delivery_store.new_delivery_id()
        existing = delivery_store.get_delivery(conn, delivery_id)
        if existing is not None:
            if existing["session_id"] != request.session_id:
                raise ValueError("Delivery does not belong to the target Session")
            return existing
        dedupe_key = (
            f"{request.platform}:{request.native_message_id}"
            if request.native_message_id
            else None
        )
        return delivery_store.insert_delivery(
            conn,
            delivery_id=delivery_id,
            session_id=request.session_id,
            priority=priority,
            state=state,
            snapshot=SessionTurnManager._delivery_snapshot(request),
            dispatch_text=str(request.content or ""),
            dedupe_key=dedupe_key,
            history_event={"kind": "admission", "priority": priority, "state": state},
        )

    def _active_identity(
        self,
        backend: str,
        session_id: str,
        logical_turn_id: str,
    ) -> tuple[str, str] | None:
        return active_steer_identity(
            self.controller,
            backend,
            session_id,
            expected_logical_turn_id=logical_turn_id,
        )

    async def _steer(self, backend: str, request: SteerRequest):
        return await steer_active_turn(self.controller, backend, request)

    async def _attempt_steer(self, backend: str, request: SteerRequest):
        try:
            return await self._steer(backend, request)
        except Exception as exc:
            logger.exception(
                "native steering outcome is unknown for Session=%s Turn=%s",
                request.target_session_id,
                request.expected_logical_turn_id,
            )
            return steer_result(
                SteerOutcome.UNKNOWN,
                reason="adapter_error",
                error_type=type(exc).__name__,
            )

    def _observe_active_delivery_turn(
        self,
        session_id: str,
    ) -> tuple[dict[str, Any] | None, tuple[str, str] | None]:
        with self._sqlite_engine().connect() as conn:
            turn = delivery_store.active_turn(conn, session_id)
        if turn is None:
            return turn, None
        identity = self._active_identity(
            str(turn["backend"]),
            session_id,
            str(turn["id"]),
        )
        persisted_native_id = str(turn.get("native_turn_id") or "").strip()
        if identity is None and turn["state"] == "active" and persisted_native_id:
            identity = (str(turn["id"]), persisted_native_id)
        if identity is not None and turn["state"] != "active":
            with self._sqlite_engine().begin() as conn:
                reserve_write_lock(conn)
                latest = delivery_store.get_turn(conn, str(turn["id"]))
                if latest is None or latest["state"] not in delivery_store.TURN_OWNER_STATES:
                    return latest, None
                bound = delivery_store.bind_native_start(
                    conn,
                    str(turn["id"]),
                    expected_version=int(latest["version"]),
                    runtime_key=latest.get("runtime_key"),
                    runtime_turn_id=latest.get("runtime_turn_id"),
                    native_turn_id=identity[1],
                )
                if bound is None:
                    return latest, None
                turn = bound
        return turn, identity

    async def deliver(
        self,
        request: DeliveryRequest,
        *,
        context: Optional["MessageContext"] = None,
    ) -> DeliveryResult:
        """Execute the private durable P0/P1/P3 ownership state machine."""

        if not request.session_id:
            raise ValueError("Session delivery requires a Session id")
        if request.priority not in {"p0", "p1", "p3"}:
            raise ValueError(f"unsupported delivery priority: {request.priority}")
        content_present = (
            request.has_content
            if request.has_content is not None
            else bool(request.content)
        )
        if not content_present and request.content == "":
            request = replace(request, content=None)
        if request.priority == "p3" and not content_present:
            raise ValueError("P3 requires content")
        backend, resolved_context = self._delivery_backend(request.session_id, context)
        if request.priority == "p3":
            return await self._admit_p3(request, backend, resolved_context)
        if request.priority == "p1":
            return await self._admit_p1(request, backend, resolved_context)
        return await self._admit_p0(request, backend, resolved_context)

    async def _admit_p3(
        self,
        request: DeliveryRequest,
        backend: str,
        context: "MessageContext",
    ) -> DeliveryResult:
        turn_id: str | None = None
        delivery_turn_id: str | None = None
        start_context: MessageContext | None = None
        delivery: dict[str, Any]
        backend_draining = backend in self._draining_backends
        with self._sqlite_engine().begin() as conn:
            reserve_write_lock(conn)
            status = conn.execute(
                select(agent_sessions.c.status).where(agent_sessions.c.id == request.session_id)
            ).scalar_one_or_none()
            if status != "active":
                existing = (
                    delivery_store.get_delivery(conn, request.delivery_id)
                    if request.delivery_id
                    else None
                )
                if existing is not None and existing["state"] == "reserved":
                    delivery_store.retire_not_written(
                        conn,
                        request.session_id,
                        str(existing["id"]),
                        reason="session_archived",
                    )
                    return DeliveryResult(str(existing["id"]), None, "retired")
                raise ValueError("Session is archived")
            active = delivery_store.active_turn(conn, request.session_id)
            delivery = self._insert_delivery(
                conn,
                request,
                priority="p3",
                state="queued" if active is not None or backend_draining else "reserved",
            )
            if delivery["state"] not in {"queued", "reserved"}:
                return DeliveryResult(
                    str(delivery["id"]),
                    str(delivery.get("message_id") or "") or None,
                    str(delivery["state"]),
                    str(delivery.get("current_target_turn_id") or "") or None,
                )
            if (active is not None or backend_draining) and delivery["state"] == "reserved":
                queue_reason = "active_turn" if active is not None else "backend_drain"
                history_event: dict[str, Any] = {
                    "kind": "queue",
                    "reason": queue_reason,
                }
                if active is not None:
                    history_event["turn_id"] = str(active["id"])
                claimed = delivery_store.cas_delivery(
                    conn,
                    str(delivery["id"]),
                    expected_version=int(delivery["version"]),
                    expected_states=("reserved",),
                    values={"state": "queued"},
                    history_event=history_event,
                )
                if claimed is None:
                    raise RuntimeError("P3 queue claim lost after writer reservation")
                delivery = claimed
            start_owner = delivery
            if (
                active is None
                and not backend_draining
                and not delivery_store.queue_is_held(conn, request.session_id)
            ):
                ordering_head = delivery_store.ordering_head(conn, request.session_id)
                if (
                    ordering_head is not None
                    and str(ordering_head["id"]) != str(delivery["id"])
                ):
                    if delivery["state"] == "reserved":
                        queued = delivery_store.cas_delivery(
                            conn,
                            str(delivery["id"]),
                            expected_version=int(delivery["version"]),
                            expected_states=("reserved",),
                            values={"state": "queued"},
                            history_event={
                                "kind": "queue",
                                "reason": "fifo_backlog",
                                "head_delivery_id": str(ordering_head["id"]),
                            },
                        )
                        if queued is None:
                            raise RuntimeError(
                                "P3 FIFO queue claim lost after writer reservation"
                            )
                        delivery = queued
                    start_owner = (
                        ordering_head
                        if ordering_head["state"] == "queued"
                        else None
                    )
            if active is None and not backend_draining and start_owner is not None:
                turn_id = delivery_store.new_turn_id()
                delivery_store.insert_turn(
                    conn,
                    turn_id=turn_id,
                    session_id=request.session_id,
                    initial_delivery_id=str(start_owner["id"]),
                    state="starting",
                    backend=backend,
                )
                claimed = delivery_store.open_start_attempt(
                    conn,
                    str(start_owner["id"]),
                    expected_version=int(start_owner["version"]),
                    turn_id=turn_id,
                    attempt_id=delivery_store.new_attempt_id(),
                )
                if claimed is None:
                    raise RuntimeError("P3 start claim lost after writer reservation")
                if str(start_owner["id"]) == str(delivery["id"]):
                    delivery = claimed
                    delivery_turn_id = turn_id
                    start_context = context
        if backend_draining:
            self._deferred_restart_sessions.setdefault(backend, set()).add(
                request.session_id
            )
        if turn_id:
            await self._start_persisted_turn(turn_id, context=start_context)
        return DeliveryResult(
            str(delivery["id"]),
            str(delivery.get("message_id") or "") or None,
            str(delivery["state"]),
            delivery_turn_id,
        )

    async def _admit_p1(
        self,
        request: DeliveryRequest,
        backend: str,
        context: "MessageContext",
    ) -> DeliveryResult:
        if request.content is None:
            return await self._promote_fifo_head(request.session_id, backend, context)

        observed, identity = self._observe_active_delivery_turn(request.session_id)
        observed_id = str((observed or {}).get("id") or "") or None
        attempt_id: str | None = None
        native_id: str | None = None
        turn_id: str | None = None
        steer_backend = backend
        delivery: dict[str, Any]
        with self._sqlite_engine().begin() as conn:
            reserve_write_lock(conn)
            status = conn.execute(
                select(agent_sessions.c.status).where(agent_sessions.c.id == request.session_id)
            ).scalar_one_or_none()
            if status != "active":
                existing = (
                    delivery_store.get_delivery(conn, request.delivery_id)
                    if request.delivery_id
                    else None
                )
                if existing is not None and existing["state"] == "reserved":
                    delivery_store.retire_not_written(
                        conn,
                        request.session_id,
                        str(existing["id"]),
                        reason="session_archived",
                    )
                    return DeliveryResult(str(existing["id"]), None, "retired")
                raise ValueError("Session is archived")
            current = delivery_store.active_turn(conn, request.session_id)
            delivery = self._insert_delivery(
                conn,
                request,
                priority="p1",
                state="reserved",
            )
            if delivery["state"] != "reserved":
                return DeliveryResult(
                    str(delivery["id"]),
                    str(delivery.get("message_id") or "") or None,
                    str(delivery["state"]),
                    str(delivery.get("current_target_turn_id") or "") or None,
                )
            same_active = bool(
                current is not None
                and observed_id
                and str(current["id"]) == observed_id
                and current["state"] == "active"
                and identity is not None
                and identity[0] == observed_id
            )
            if same_active:
                attempt_id = delivery_store.new_attempt_id()
                native_id = str(identity[1])
                turn_id = observed_id
                steer_backend = str(current["backend"])
                claimed = delivery_store.open_steer_attempt(
                    conn,
                    str(delivery["id"]),
                    expected_version=int(delivery["version"]),
                    turn_id=turn_id,
                    attempt_id=attempt_id,
                    expected_native_turn_id=native_id,
                )
                if claimed is None:
                    raise RuntimeError("P1 steer claim lost after writer reservation")
                delivery = claimed
            elif current is None:
                turn_id = delivery_store.new_turn_id()
                if observed_id:
                    delivery = delivery_store.cas_delivery(
                        conn,
                        str(delivery["id"]),
                        expected_version=int(delivery["version"]),
                        expected_states=("reserved",),
                        values={},
                        history_event={
                            "kind": "admission",
                            "turn_id": observed_id,
                            "outcome": "observed_turn_settled_before_claim",
                        },
                    ) or delivery
                delivery_store.insert_turn(
                    conn,
                    turn_id=turn_id,
                    session_id=request.session_id,
                    initial_delivery_id=str(delivery["id"]),
                    state="starting",
                    backend=backend,
                )
                attempt_id = delivery_store.new_attempt_id()
                claimed = delivery_store.open_start_attempt(
                    conn,
                    str(delivery["id"]),
                    expected_version=int(delivery["version"]),
                    turn_id=turn_id,
                    attempt_id=attempt_id,
                )
                if claimed is None:
                    raise RuntimeError("P1 start claim lost after writer reservation")
                delivery = claimed
            elif observed_id and str(current["id"]) == observed_id:
                claimed = delivery_store.cas_delivery(
                    conn,
                    str(delivery["id"]),
                    expected_version=int(delivery["version"]),
                    expected_states=("reserved",),
                    values={
                        "state": "pending_steer",
                        "current_target_turn_id": str(current["id"]),
                    },
                )
                if claimed is None:
                    raise RuntimeError("P1 pending steer claim lost after writer reservation")
                delivery = claimed
                turn_id = str(current["id"])
            else:
                claimed = delivery_store.cas_delivery(
                    conn,
                    str(delivery["id"]),
                    expected_version=int(delivery["version"]),
                    expected_states=("reserved",),
                    values={"priority": "p3", "state": "queued"},
                    history_event={
                        "kind": "steer",
                        "turn_id": observed_id,
                        "outcome": "target_changed_before_claim",
                        "current_turn_id": str(current["id"]),
                    },
                )
                if claimed is None:
                    raise RuntimeError("P1 stale-target fallback claim lost")
                delivery = claimed

        if delivery["state"] == "start_attempting" and turn_id:
            await self._start_persisted_turn(turn_id, context=context)
        elif delivery["state"] == "steering" and turn_id and attempt_id and native_id:
            receipt = await self._attempt_steer(
                steer_backend,
                SteerRequest(
                    target_session_id=request.session_id,
                    expected_logical_turn_id=turn_id,
                    expected_native_turn_id=native_id,
                    text=str(delivery.get("dispatch_text") or ""),
                ),
            )
            return await self._finish_steer(str(delivery["id"]), receipt, context=context)
        return DeliveryResult(str(delivery["id"]), None, str(delivery["state"]), turn_id)

    async def _promote_fifo_head(
        self,
        session_id: str,
        backend: str,
        context: "MessageContext",
    ) -> DeliveryResult:
        with self._sqlite_engine().connect() as conn:
            observed_head = delivery_store.ordering_head(conn, session_id)
        if observed_head is None:
            return DeliveryResult(None, None, "empty")
        observed_owner_id = str(observed_head["id"])
        if observed_head["state"] != "queued":
            return DeliveryResult(
                observed_owner_id,
                None,
                "refused",
                reason="ordering_fence",
            )
        observed_turn, identity = self._observe_active_delivery_turn(session_id)
        observed_turn_id = str((observed_turn or {}).get("id") or "") or None
        delivery_id = observed_owner_id
        turn_id: str | None = None
        attempt_id: str | None = None
        native_id: str | None = None
        steer_backend = backend
        claimed: dict[str, Any] | None = None
        with self._sqlite_engine().begin() as conn:
            reserve_write_lock(conn)
            status = conn.execute(
                select(agent_sessions.c.status).where(agent_sessions.c.id == session_id)
            ).scalar_one_or_none()
            if status != "active":
                return DeliveryResult(observed_owner_id, None, "refused", reason="session_archived")
            current_head = delivery_store.ordering_head(conn, session_id)
            if current_head is None or str(current_head["id"]) != observed_owner_id:
                return DeliveryResult(
                    delivery_id,
                    None,
                    "refused",
                    reason="stale_head",
                )
            if current_head["state"] != "queued":
                return DeliveryResult(delivery_id, None, "refused", reason="ordering_fence")
            delivery_store.set_queue_hold(conn, session_id, held=False)
            current_turn = delivery_store.active_turn(conn, session_id)
            if current_turn is None:
                turn_id = delivery_store.new_turn_id()
                delivery_store.insert_turn(
                    conn,
                    turn_id=turn_id,
                    session_id=session_id,
                    initial_delivery_id=delivery_id,
                    state="starting",
                    backend=backend,
                )
                attempt_id = delivery_store.new_attempt_id()
                claimed = delivery_store.open_start_attempt(
                    conn,
                    delivery_id,
                    expected_version=int(current_head["version"]),
                    turn_id=turn_id,
                    attempt_id=attempt_id,
                )
                if claimed is None:
                    raise RuntimeError("FIFO head start CAS lost after writer reservation")
            elif (
                observed_turn_id
                and str(current_turn["id"]) == observed_turn_id
                and current_turn["state"] == "active"
                and identity is not None
                and identity[0] == observed_turn_id
            ):
                turn_id = observed_turn_id
                attempt_id = delivery_store.new_attempt_id()
                native_id = str(identity[1])
                steer_backend = str(current_turn["backend"])
                claimed = delivery_store.open_steer_attempt(
                    conn,
                    delivery_id,
                    expected_version=int(current_head["version"]),
                    turn_id=turn_id,
                    attempt_id=attempt_id,
                    expected_native_turn_id=native_id,
                )
                if claimed is None:
                    raise RuntimeError("FIFO head steering CAS lost after writer reservation")
            elif observed_turn_id and str(current_turn["id"]) == observed_turn_id:
                claimed = delivery_store.cas_delivery(
                    conn,
                    delivery_id,
                    expected_version=int(current_head["version"]),
                    expected_states=("queued",),
                    values={
                        "state": "pending_steer",
                        "current_target_turn_id": str(current_turn["id"]),
                    },
                )
                turn_id = str(current_turn["id"])
            else:
                return DeliveryResult(
                    delivery_id,
                    None,
                    "refused",
                    reason="stale_turn",
                )

        if claimed is None:
            return DeliveryResult(delivery_id, None, "refused", reason="claim_lost")
        if claimed["state"] == "start_attempting" and turn_id:
            await self._start_persisted_turn(turn_id, context=context)
        elif claimed["state"] == "steering" and turn_id and attempt_id and native_id:
            receipt = await self._attempt_steer(
                steer_backend,
                SteerRequest(
                    target_session_id=session_id,
                    expected_logical_turn_id=turn_id,
                    expected_native_turn_id=native_id,
                    text=str(claimed.get("dispatch_text") or ""),
                ),
            )
            return await self._finish_steer(delivery_id, receipt, context=context)
        return DeliveryResult(delivery_id, None, str(claimed["state"]), turn_id)

    async def _finish_steer(
        self,
        delivery_id: str,
        receipt: Any,
        *,
        context: Optional["MessageContext"],
    ) -> DeliveryResult:
        outcome = getattr(receipt, "outcome", SteerOutcome.UNKNOWN)
        outcome_value = str(getattr(outcome, "value", outcome))
        body = {
            "reason": getattr(receipt, "reason", None),
            "details": dict(getattr(receipt, "details", {}) or {}),
        }
        start_turn_id: str | None = None
        materialized = False
        saved: dict[str, Any] | None = None
        try:
            with self._sqlite_engine().begin() as conn:
                reserve_write_lock(conn)
                delivery = delivery_store.get_delivery(conn, delivery_id)
                if delivery is None:
                    return DeliveryResult(
                        delivery_id,
                        None,
                        "reconciling_steer",
                        reason="missing_delivery",
                    )
                target_turn_id = str(delivery.get("current_target_turn_id") or "")
                if outcome_value == SteerOutcome.ACCEPTED.value:
                    saved = delivery_store.materialize_acceptance(
                        conn,
                        delivery_id=delivery_id,
                        expected_attempt_id=str(delivery.get("current_attempt_id") or "") or None,
                        accepted_turn_id=target_turn_id,
                        evidence={"kind": "steer_receipt", "receipt": body},
                    )
                    if saved is None:
                        return DeliveryResult(
                            delivery_id,
                            None,
                            "reconciling_steer",
                            target_turn_id or None,
                            "receipt_cas_lost",
                        )
                    materialized = True
                if outcome_value == SteerOutcome.UNKNOWN.value:
                    saved = delivery_store.mark_attempt_unknown(
                        conn,
                        delivery_id,
                        expected_version=int(delivery["version"]),
                        receipt=body,
                    )
                    return DeliveryResult(
                        delivery_id,
                        None,
                        "reconciling_steer",
                        target_turn_id or None,
                        None if saved is not None else "receipt_cas_lost",
                    )
                if not materialized:
                    current = delivery_store.active_turn(conn, str(delivery["session_id"]))
                    session_status = conn.execute(
                        select(agent_sessions.c.status).where(
                            agent_sessions.c.id == str(delivery["session_id"])
                        )
                    ).scalar_one_or_none()
                    next_state = (
                        "retired"
                        if session_status != "active"
                        else "reserved"
                        if current is None
                        else "queued"
                    )
                    saved = delivery_store.record_definitive_attempt(
                        conn,
                        delivery_id,
                        expected_version=int(delivery["version"]),
                        expected_states=("steering",),
                        outcome=outcome_value,
                        next_state=next_state,
                        next_priority="p3",
                        receipt=body,
                    )
                    if saved is None:
                        return DeliveryResult(
                            delivery_id,
                            None,
                            "reconciling_steer",
                            target_turn_id or None,
                            "fallback_cas_lost",
                        )
                if not materialized and saved is not None and saved["state"] == "reserved":
                    start_turn_id = delivery_store.new_turn_id()
                    delivery_store.insert_turn(
                        conn,
                        turn_id=start_turn_id,
                        session_id=str(delivery["session_id"]),
                        initial_delivery_id=delivery_id,
                        state="starting",
                        backend=str(
                            (delivery_store.get_turn(conn, target_turn_id) or {}).get(
                                "backend"
                            )
                            or self._context_backend(context)
                            or ""
                        ),
                    )
                    saved = delivery_store.open_start_attempt(
                        conn,
                        delivery_id,
                        expected_version=int(saved["version"]),
                        turn_id=start_turn_id,
                        attempt_id=delivery_store.new_attempt_id(),
                    )
                    if saved is None:
                        raise RuntimeError("P1 fallback start claim lost")
        except Exception:
            logger.exception("failed to persist steering receipt for delivery=%s", delivery_id)
            return DeliveryResult(
                delivery_id,
                None,
                "reconciling_steer",
                reason="receipt_persistence_lost",
            )

        if materialized:
            self._publish_materialized_delivery(delivery_id)
            return DeliveryResult(delivery_id, delivery_id, "accepted", target_turn_id or None)
        if start_turn_id:
            await self._start_persisted_turn(start_turn_id, context=context)
        return DeliveryResult(
            delivery_id,
            None,
            str((saved or {}).get("state") or "reconciling_steer"),
            start_turn_id,
        )

    async def _admit_p0(
        self,
        request: DeliveryRequest,
        backend: str,
        context: "MessageContext",
    ) -> DeliveryResult:
        delivery_id: str | None = None
        successor_id: str | None = None
        interrupt_target_id: str | None = None
        should_interrupt = False
        joined = False
        with self._sqlite_engine().begin() as conn:
            reserve_write_lock(conn)
            session_status = conn.execute(
                select(agent_sessions.c.status).where(
                    agent_sessions.c.id == request.session_id
                )
            ).scalar_one_or_none()
            current = delivery_store.active_turn(conn, request.session_id)
            if request.content is not None and session_status != "active":
                existing = (
                    delivery_store.get_delivery(conn, request.delivery_id)
                    if request.delivery_id
                    else None
                )
                if existing is not None and existing["state"] == "reserved":
                    delivery_store.retire_not_written(
                        conn,
                        request.session_id,
                        str(existing["id"]),
                        reason="session_archived",
                    )
                    return DeliveryResult(str(existing["id"]), None, "retired")
                raise ValueError("Session is archived")
            current_id = str((current or {}).get("id") or "") or None
            if current is None:
                if request.content is None:
                    return DeliveryResult(None, None, "settled", reason="not_active")
                delivery = self._insert_delivery(
                    conn,
                    request,
                    priority="p0",
                    state="reserved",
                )
                delivery_id = str(delivery["id"])
                successor_id = delivery_store.new_turn_id()
                delivery_store.insert_turn(
                    conn,
                    turn_id=successor_id,
                    session_id=request.session_id,
                    initial_delivery_id=delivery_id,
                    state="starting",
                    backend=backend,
                )
                claimed = delivery_store.open_start_attempt(
                    conn,
                    delivery_id,
                    expected_version=int(delivery["version"]),
                    turn_id=successor_id,
                    attempt_id=delivery_store.new_attempt_id(),
                )
                if claimed is None:
                    raise RuntimeError("idle P0 start claim lost")
            else:
                interrupt_target_id = current_id
                delivery = None
                queue_was_held = delivery_store.queue_is_held(
                    conn,
                    request.session_id,
                )
                control_in_progress = current.get("control_state") in {
                    "pending",
                    "interrupting",
                    "waiting_terminal",
                    "reconciling",
                }
                if request.content is not None:
                    delivery = self._insert_delivery(
                        conn,
                        request,
                        priority="p0",
                        state="interrupt_waiting",
                    )
                    delivery_id = str(delivery["id"])
                    if delivery["state"] == "reserved":
                        claimed_delivery = delivery_store.cas_delivery(
                            conn,
                            delivery_id,
                            expected_version=int(delivery["version"]),
                            expected_states=("reserved",),
                            values={"priority": "p0", "state": "interrupt_waiting"},
                            history_event={
                                "kind": "interrupt_join",
                                "target_turn_id": current_id,
                                "outcome": "control_slot_candidate",
                            },
                        )
                        if claimed_delivery is None:
                            raise RuntimeError("P0 successor reservation claim lost")
                        delivery = claimed_delivery
                    elif delivery["state"] != "interrupt_waiting":
                        return DeliveryResult(
                            delivery_id,
                            str(delivery.get("message_id") or "") or None,
                            str(delivery["state"]),
                            str(delivery.get("current_target_turn_id") or "") or None,
                        )
                # Persist no-flush intent before Stop. A definitive refusal rolls
                # it back atomically with the control receipt below.
                delivery_store.set_queue_hold(conn, request.session_id, held=True)
                if control_in_progress:
                    joined = True
                    if delivery is not None:
                        # Only the control-slot winner may replace the active Turn.
                        # A content loser remains one FIFO submission and never calls Stop.
                        queued = delivery_store.cas_delivery(
                            conn,
                            delivery_id,
                            expected_version=int(delivery["version"]),
                            expected_states=("interrupt_waiting",),
                            values={"priority": "p3", "state": "queued"},
                            history_event={
                                "kind": "interrupt_join",
                                "target_turn_id": current_id,
                                "outcome": "coalesced_to_queue",
                            },
                        )
                        if queued is None:
                            raise RuntimeError("concurrent P0 loser queue claim lost")
                else:
                    if delivery_id is not None:
                        successor_id = delivery_store.new_turn_id()
                        delivery_store.insert_turn(
                            conn,
                            turn_id=successor_id,
                            session_id=request.session_id,
                            initial_delivery_id=delivery_id,
                            state="waiting",
                            backend=backend,
                        )
                    attempt_id = delivery_store.new_attempt_id()
                    claimed_control = delivery_store.cas_turn(
                        conn,
                        current_id,
                        expected_version=int(current["version"]),
                        expected_states=(str(current["state"]),),
                        values={
                            "control_state": (
                                "interrupting" if current["state"] == "active" else "pending"
                            ),
                            "control_mode": "replace" if delivery_id else "stop_only",
                            "control_attempt_id": attempt_id,
                            "control_expected_native_turn_id": current.get("native_turn_id"),
                            "control_receipt_outcome": None,
                            "control_receipt_json": json.dumps(
                                {"queue_hold_was_held": queue_was_held},
                                sort_keys=True,
                            ),
                            "control_successor_delivery_id": delivery_id,
                            "control_successor_turn_id": successor_id,
                        },
                    )
                    if claimed_control is None:
                        raise RuntimeError("P0 control-slot claim lost")
                    should_interrupt = current["state"] == "active"

        if current is None and successor_id:
            await self._start_persisted_turn(successor_id, context=context)
            return DeliveryResult(delivery_id, None, "start_attempting", successor_id)
        if joined:
            return DeliveryResult(
                delivery_id,
                None,
                "queued" if delivery_id else "interrupt_waiting",
                interrupt_target_id,
                "joined_existing_interrupt",
            )
        if not should_interrupt:
            return DeliveryResult(
                delivery_id,
                None,
                "interrupt_waiting",
                interrupt_target_id,
            )
        interrupted = await self._interrupt_durable_turn(
            request.session_id,
            interrupt_target_id,
        )
        return DeliveryResult(
            delivery_id,
            None,
            str(interrupted.get("state") or "reconciling"),
            interrupt_target_id,
            interrupted.get("reason"),
        )

    async def _interrupt_durable_turn(
        self,
        session_id: str,
        logical_turn_id: str | None,
    ) -> dict[str, Any]:
        turn = self.in_flight.get(session_id)
        runtime_turn = turn
        if (
            turn is None
            or not logical_turn_id
            or turn.logical_turn_id != logical_turn_id
            or turn.task.done()
        ):
            runtime_turn = None
        stop_context = runtime_turn.context if runtime_turn is not None else None
        unavailable_reason = "runtime_owner_unavailable"
        if stop_context is None and logical_turn_id:
            with self._sqlite_engine().connect() as conn:
                durable_turn = delivery_store.get_turn(conn, logical_turn_id)
                active_turn = delivery_store.active_turn(conn, session_id)
            exact_owner = bool(
                durable_turn is not None
                and active_turn is not None
                and str(active_turn["id"]) == logical_turn_id
                and durable_turn["state"] == "active"
                and str(durable_turn.get("native_turn_id") or "").strip()
            )
            identity = (
                self._active_identity(
                    str(durable_turn["backend"]),
                    session_id,
                    logical_turn_id,
                )
                if exact_owner
                else None
            )
            if identity == (
                logical_turn_id,
                str((durable_turn or {}).get("native_turn_id") or "").strip(),
            ):
                try:
                    stop_context = self._delivery_context(session_id)
                except Exception:
                    logger.exception(
                        "failed to rebuild restored interrupt context for Session=%s",
                        session_id,
                    )
                    unavailable_reason = "runtime_context_unavailable"
                else:
                    if stop_context.platform_specific is None:
                        stop_context.platform_specific = {}
                    stop_context.platform_specific.update(
                        {
                            "turn_token": logical_turn_id,
                            "agent_runtime_turn_key": str(
                                durable_turn.get("runtime_key") or ""
                            ),
                            "agent_runtime_turn_token": str(
                                durable_turn.get("runtime_turn_id") or ""
                            ),
                        }
                    )
        if stop_context is None:
            with self._sqlite_engine().begin() as conn:
                reserve_write_lock(conn)
                durable_turn = delivery_store.get_turn(conn, str(logical_turn_id or ""))
                if durable_turn is None:
                    return {"state": "reconciling", "reason": "turn_missing"}
                if durable_turn.get("control_state") == "interrupting":
                    saved = delivery_store.cas_turn(
                        conn,
                        str(logical_turn_id),
                        expected_version=int(durable_turn["version"]),
                        expected_states=(str(durable_turn["state"]),),
                        values={
                            "control_state": "reconciling",
                            "control_receipt_outcome": "unknown",
                            "control_receipt_json": json.dumps(
                                {"reason": unavailable_reason},
                                sort_keys=True,
                            ),
                        },
                    )
                    if saved is None:
                        return {"state": "reconciling", "reason": "receipt_cas_lost"}
                return {"state": "reconciling", "reason": unavailable_reason}
        if stop_context.platform_specific is None:
            stop_context.platform_specific = {}
        stop_context.platform_specific["suppress_stop_no_active_notice"] = True
        if runtime_turn is not None:
            with self._sqlite_engine().connect() as conn:
                control_owner = delivery_store.get_turn(conn, str(logical_turn_id or ""))
            replaces_turn = bool(
                control_owner is not None
                and control_owner.get("control_mode") == "replace"
                and control_owner.get("control_successor_turn_id")
            )
            runtime_turn.cancel_settled_by = SETTLED_BY_STOPPED
            runtime_turn.flush_on_cancel = replaces_turn
            runtime_turn.stop_no_flush = not replaces_turn
        stop_error = False
        try:
            stopped = bool(
                await self.controller.command_handler.handle_stop(stop_context)
            )
        except Exception:
            logger.exception("durable P0 interrupt failed for Session=%s", session_id)
            stopped = False
            stop_error = True
        if not stopped and runtime_turn is not None:
            runtime_turn.cancel_settled_by = None
        terminal_proven = False
        with self._sqlite_engine().begin() as conn:
            reserve_write_lock(conn)
            durable_turn = delivery_store.get_turn(conn, str(logical_turn_id or ""))
            if durable_turn is None:
                return {"state": "reconciling", "reason": "turn_missing"}
            if durable_turn.get("control_state") != "interrupting":
                return {
                    "state": str(durable_turn.get("control_state") or "reconciling"),
                    "reason": None,
                }
            if stopped:
                saved = delivery_store.cas_turn(
                    conn,
                    str(logical_turn_id),
                    expected_version=int(durable_turn["version"]),
                    expected_states=(str(durable_turn["state"]),),
                    values={
                        "control_state": "waiting_terminal",
                        "control_receipt_outcome": "accepted",
                        "control_receipt_json": json.dumps(
                            {"kind": "stop_receipt"}, sort_keys=True
                        ),
                    },
                )
                return {
                    "state": "waiting_terminal" if saved is not None else "reconciling",
                    "reason": None if saved is not None else "receipt_cas_lost",
                }
            reason = str(
                (getattr(stop_context, "platform_specific", None) or {}).get(
                    "stop_failure_reason"
                )
                or ""
            )
            definitive = not stop_error and reason in {"not_active", "refused"}
            terminal_proven = definitive and reason == "not_active"
            fallback_state: str | None = None
            successor_delivery_id = str(
                durable_turn.get("control_successor_delivery_id") or ""
            )
            successor_turn_id = str(
                durable_turn.get("control_successor_turn_id") or ""
            )
            if definitive and not terminal_proven and successor_delivery_id and successor_turn_id:
                successor = delivery_store.get_turn(conn, successor_turn_id)
                successor_delivery = delivery_store.get_delivery(
                    conn,
                    successor_delivery_id,
                )
                if (
                    successor is not None
                    and successor["state"] == "waiting"
                    and successor_delivery is not None
                    and successor_delivery["state"] == "interrupt_waiting"
                ):
                    terminalized = self._write_terminal_snapshot(
                        conn,
                        successor_turn_id,
                        outcome="not_written",
                        settled_by="interrupt_refused",
                        evidence_kind="definitive_stop_receipt",
                        evidence={"reason": reason},
                    )
                    queued = delivery_store.cas_delivery(
                        conn,
                        successor_delivery_id,
                        expected_version=int(successor_delivery["version"]),
                        expected_states=("interrupt_waiting",),
                        values={"state": "queued", "priority": "p3"},
                        history_event={
                            "kind": "interrupt_join",
                            "attempt_id": durable_turn.get("control_attempt_id"),
                            "turn_id": str(durable_turn["id"]),
                            "outcome": "refused_to_p3",
                            "receipt": {"reason": reason},
                        },
                    )
                    if not terminalized.get("changed") or queued is None:
                        raise RuntimeError("definitive P0 refusal fallback lost")
                    fallback_state = "queued"
            if definitive and not terminal_proven:
                try:
                    control_context = json.loads(
                        str(durable_turn.get("control_receipt_json") or "{}")
                    )
                except (TypeError, ValueError):
                    control_context = {}
                delivery_store.set_queue_hold(
                    conn,
                    session_id,
                    held=bool(control_context.get("queue_hold_was_held")),
                )
            saved = delivery_store.cas_turn(
                conn,
                str(logical_turn_id),
                expected_version=int(durable_turn["version"]),
                expected_states=(str(durable_turn["state"]),),
                values={
                    "control_state": (
                        "waiting_terminal"
                        if terminal_proven
                        else "refused"
                        if definitive
                        else "reconciling"
                    ),
                    "control_receipt_outcome": (
                        "not_active"
                        if terminal_proven
                        else "refused"
                        if definitive
                        else "unknown"
                    ),
                    "control_receipt_json": json.dumps(
                        {"reason": reason or "stop_error"}, sort_keys=True
                    ),
                },
            )
            receipt_result = {
                "state": fallback_state or ("refused" if definitive else "reconciling"),
                "reason": (
                    reason
                    if definitive
                    else "stop_unknown"
                    if saved is not None
                    else "receipt_cas_lost"
                ),
            }
        if terminal_proven and saved is not None:
            terminal = self._terminalize_durable_turn(
                str(logical_turn_id),
                "canceled",
                settled_by="adapter_not_active",
                evidence_kind="stop_not_active",
                evidence={"reason": "not_active"},
                hold_queue=True,
            )
            projected = self.in_flight.get(session_id)
            if (
                projected is not None
                and projected.logical_turn_id == logical_turn_id
                and not projected.task.done()
            ):
                projected.cancel_settled_by = "adapter_not_active"
                projected.task.cancel()
                await asyncio.gather(projected.task, return_exceptions=True)
            if self.in_flight.get(session_id) is projected and projected is not None:
                self.in_flight.pop(session_id, None)
                from core.inbox_events import bus

                bus.publish("turn.end", {"session_id": session_id})
            if self.controller is not None:
                self.controller.set_agent_status(session_id, "idle")
            successor_turn_id = terminal.get("successor_turn_id")
            if successor_turn_id:
                await self._start_persisted_turn(str(successor_turn_id))
            return {
                "state": "start_attempting" if successor_turn_id else "settled",
                "reason": "not_active",
            }
        return receipt_result

    async def _start_persisted_turn(
        self,
        turn_id: str,
        *,
        context: Optional["MessageContext"] = None,
    ) -> bool:
        with self._sqlite_engine().begin() as conn:
            reserve_write_lock(conn)
            turn = delivery_store.get_turn(conn, turn_id)
            if turn is None or turn["state"] != "starting":
                return False
            backend = str(turn.get("backend") or "").strip()
            if backend in self._draining_backends:
                self._deferred_restart_sessions.setdefault(backend, set()).add(
                    str(turn["session_id"])
                )
                return False
            delivery = delivery_store.delivery_for_turn(conn, turn_id)
            if (
                delivery is None
                or delivery["state"] != "start_attempting"
                or delivery.get("current_attempt_kind") != "start"
                or delivery.get("current_target_turn_id") != turn_id
                or not delivery.get("current_attempt_id")
            ):
                logger.error("durable Turn has no exact start-attempt owner: %s", turn_id)
                return False
            attempt_id = str(delivery["current_attempt_id"])
        try:
            resolved = context or self._delivery_context(str(turn["session_id"]))
        except Exception:
            logger.exception("durable native start failed before dispatch for Turn=%s", turn_id)
            self._terminalize_durable_turn(
                turn_id,
                "not_written",
                settled_by="pre_write_failure",
                evidence_kind="context_build_failed",
            )
            return False
        archived_before_dispatch = False
        run_terminal_before_dispatch = False
        run_claim_refused = False
        with self._sqlite_engine().begin() as conn:
            reserve_write_lock(conn)
            latest = delivery_store.get_turn(conn, turn_id)
            session_status = conn.execute(
                select(agent_sessions.c.status).where(
                    agent_sessions.c.id == str(turn["session_id"])
                )
            ).scalar_one_or_none()
            if latest is None or latest["state"] != "starting" or latest.get(
                "initial_delivery_id"
            ) != delivery.get("id"):
                return False
            archived_before_dispatch = session_status != "active"
            run_id = delivery_store.owned_agent_run_id(delivery)
            if not archived_before_dispatch and run_id:
                run_row = conn.execute(
                    select(agent_runs.c.status, agent_runs.c.metadata_json).where(
                        agent_runs.c.id == run_id
                    )
                ).mappings().first()
                if run_row is not None:
                    run_status = normalize_run_status(run_row["status"])
                    if run_status == "queued":
                        if _run_metadata_holds_delivery(run_row["metadata_json"]):
                            from storage.background import (
                                claim_queued_runs_for_workbench_in_connection,
                            )

                            run_claim_refused = (
                                claim_queued_runs_for_workbench_in_connection(
                                    conn,
                                    [run_id],
                                )
                                != [run_id]
                            )
                        else:
                            run_claim_refused = True
                    elif run_status != "running":
                        run_terminal_before_dispatch = True
        if archived_before_dispatch:
            self._terminalize_durable_turn(
                turn_id,
                "not_written",
                settled_by="session_archive",
                evidence_kind="archive_won_before_native_dispatch",
            )
            return False
        if run_terminal_before_dispatch:
            self._terminalize_durable_turn(
                turn_id,
                "not_written",
                settled_by="agent_run_terminal",
                evidence_kind="agent_run_terminal_before_native_dispatch",
            )
            return False
        if run_claim_refused:
            self._terminalize_durable_turn(
                turn_id,
                "not_written",
                settled_by="agent_run_claim_refused",
                evidence_kind="agent_run_claim_refused_before_native_dispatch",
            )
            return False
        try:
            delivery_payload = self._hydrate_delivery_context(resolved, delivery)
            metadata = delivery_payload.get("metadata") or {}
            provenance = metadata.get(SCHEDULED_PROVENANCE_KEY)
            source = SOURCE_HUMAN
            if isinstance(provenance, dict):
                source = SOURCE_SCHEDULED
                native_message_id = str(provenance.get("message_id") or "").strip()
                resolved.message_id = native_message_id or str(delivery["id"])
                preserved = provenance.get("platform_specific")
                if isinstance(preserved, dict):
                    if resolved.platform_specific is None:
                        resolved.platform_specific = {}
                    resolved.platform_specific.update(preserved)
            else:
                resolved.message_id = str(delivery["id"])
            text = str(delivery.get("dispatch_text") or "")
            await self._run(
                str(turn["session_id"]),
                resolved,
                text,
                source=source,
                logical_turn_id=turn_id,
                delivery_id=str((delivery or {}).get("id") or "") or None,
                durable_preallocated=True,
            )
            return True
        except Exception:
            logger.exception("durable native start became ambiguous for Turn=%s", turn_id)
            with self._sqlite_engine().begin() as conn:
                reserve_write_lock(conn)
                latest = delivery_store.get_delivery(conn, str(delivery["id"]))
                if (
                    latest is not None
                    and latest["state"] == "start_attempting"
                    and latest.get("current_attempt_id") == attempt_id
                ):
                    delivery_store.mark_attempt_unknown(
                        conn,
                        expected_version=int(latest["version"]),
                        delivery_id=str(delivery["id"]),
                        receipt={"reason": "dispatch_may_have_written"},
                    )
            return False

    async def drain_delivery_queue(self, session_id: str) -> bool:
        turn_id: str | None = None
        with self._sqlite_engine().begin() as conn:
            reserve_write_lock(conn)
            if delivery_store.active_turn(conn, session_id) is not None:
                return False
            backend = str(
                conn.execute(
                    select(agent_sessions.c.agent_backend).where(
                        agent_sessions.c.id == session_id
                    )
                ).scalar_one_or_none()
                or ""
            ).strip()
            if not backend:
                raise RuntimeError(f"Session {session_id} has no resolved backend")
            if backend in self._draining_backends:
                self._deferred_restart_sessions.setdefault(backend, set()).add(
                    session_id
                )
                return False
            if delivery_store.queue_is_held(conn, session_id):
                return False
            head = delivery_store.claimable_fifo_head(conn, session_id)
            if head is None:
                return False
            turn_id = delivery_store.new_turn_id()
            delivery_store.insert_turn(
                conn,
                turn_id=turn_id,
                session_id=session_id,
                initial_delivery_id=str(head["id"]),
                state="starting",
                backend=backend,
            )
            claimed = delivery_store.open_start_attempt(
                conn,
                str(head["id"]),
                expected_version=int(head["version"]),
                turn_id=turn_id,
                attempt_id=delivery_store.new_attempt_id(),
            )
            if claimed is None:
                raise RuntimeError("FIFO drain CAS lost after writer reservation")
        return await self._start_persisted_turn(turn_id)

    @staticmethod
    def _write_terminal_snapshot(
        conn: Connection,
        turn_id: str,
        *,
        outcome: str,
        settled_by: str | None,
        evidence_kind: str,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The only low-level terminal write; callers add reconciliation around it."""

        return delivery_store.terminalize_turn(
            conn,
            turn_id,
            outcome=outcome,
            settled_by=settled_by,
            evidence_kind=evidence_kind,
            evidence=evidence,
        )

    def _terminalize_durable_turn(
        self,
        turn_id: str,
        outcome: str,
        *,
        settled_by: str | None = None,
        evidence_kind: str = "terminal_reconciliation",
        evidence: dict[str, Any] | None = None,
        hold_queue: bool = False,
    ) -> dict[str, Any]:
        if not self._durable_schema_available():
            return {
                "changed": False,
                "successor_turn_id": None,
                "delivery_id": None,
                "preserve_queue": False,
            }
        result: dict[str, Any]
        materialized_id: str | None = None
        with self._sqlite_engine().begin() as conn:
            reserve_write_lock(conn)
            turn = delivery_store.get_turn(conn, turn_id)
            if turn is None or turn["state"] == "terminal":
                result = {
                    "changed": False,
                    "successor_turn_id": None,
                    "delivery_id": None,
                    "preserve_queue": delivery_store.queue_is_held(
                        conn, str((turn or {}).get("session_id") or "")
                    ),
                }
            else:
                if outcome not in {"completed", "failed", "canceled", "not_written"}:
                    raise ValueError(f"invalid semantic Turn outcome: {outcome}")
                session_id = str(turn["session_id"])
                session_status = conn.execute(
                    select(agent_sessions.c.status).where(agent_sessions.c.id == session_id)
                ).scalar_one_or_none()
                initial = delivery_store.get_delivery(conn, str(turn["initial_delivery_id"]))
                if initial is not None and initial["state"] in {
                    "start_attempting",
                    "reconciling_start",
                }:
                    if outcome == "not_written":
                        owned_run_terminal = False
                        run_id = delivery_store.owned_agent_run_id(initial)
                        if run_id:
                            run_status = conn.execute(
                                select(agent_runs.c.status).where(agent_runs.c.id == run_id)
                            ).scalar_one_or_none()
                            owned_run_terminal = bool(
                                run_status
                                and normalize_run_status(run_status) not in {"queued", "running"}
                            )
                        next_state = (
                            "queued"
                            if session_status == "active" and not owned_run_terminal
                            else "retired"
                        )
                        definitive = delivery_store.record_definitive_attempt(
                            conn,
                            str(initial["id"]),
                            expected_version=int(initial["version"]),
                            expected_states=(str(initial["state"]),),
                            outcome="not_written",
                            next_state=next_state,
                            next_priority="p3",
                            receipt={"kind": evidence_kind, **(evidence or {})},
                        )
                        if definitive is None:
                            raise RuntimeError("terminal no-write evidence lost its Delivery CAS")
                    else:
                        accepted = delivery_store.materialize_acceptance(
                            conn,
                            delivery_id=str(initial["id"]),
                            expected_attempt_id=str(initial.get("current_attempt_id") or "") or None,
                            accepted_turn_id=turn_id,
                            evidence={"kind": "terminal_proof", "outcome": outcome},
                        )
                        if accepted is None:
                            raise RuntimeError(
                                "terminal evidence could not materialize initial Delivery"
                            )
                        materialized_id = str(initial["id"])
                settled = self._write_terminal_snapshot(
                    conn,
                    turn_id,
                    outcome=outcome,
                    settled_by=settled_by,
                    evidence_kind=evidence_kind,
                    evidence=evidence,
                )
                if not settled.get("changed"):
                    result = {
                        "changed": False,
                        "successor_turn_id": None,
                        "delivery_id": materialized_id,
                        "preserve_queue": delivery_store.queue_is_held(conn, session_id),
                    }
                else:
                    if hold_queue:
                        delivery_store.set_queue_hold(conn, session_id, held=True)

                    # A never-called steer has definitive negative evidence once
                    # its exact target Turn settles. In-flight and unknown attempts
                    # remain untouched because they may already have written.
                    pending_rows = conn.execute(
                        select(delivery_store.message_deliveries).where(
                            delivery_store.message_deliveries.c.session_id == session_id,
                            delivery_store.message_deliveries.c.state == "pending_steer",
                            delivery_store.message_deliveries.c.current_target_turn_id == turn_id,
                        )
                    ).mappings()
                    for pending_row in pending_rows:
                        fallback = delivery_store.record_definitive_attempt(
                            conn,
                            str(pending_row["id"]),
                            expected_version=int(pending_row["version"]),
                            expected_states=("pending_steer",),
                            outcome="not_active",
                            next_state=("queued" if session_status == "active" else "retired"),
                            next_priority="p3",
                            receipt={"kind": "target_turn_terminal", "turn_id": turn_id},
                        )
                        if fallback is None:
                            raise RuntimeError("pending steer terminal fallback lost")

                    successor_turn_id = str(turn.get("control_successor_turn_id") or "")
                    successor_delivery_id = str(
                        turn.get("control_successor_delivery_id") or ""
                    )
                    claimed_successor: str | None = None
                    if (
                        successor_turn_id
                        and successor_delivery_id
                        and turn.get("control_state")
                        in {"pending", "interrupting", "waiting_terminal", "reconciling"}
                    ):
                        successor = delivery_store.get_turn(conn, successor_turn_id)
                        successor_delivery = delivery_store.get_delivery(
                            conn, successor_delivery_id
                        )
                        if (
                            successor is not None
                            and successor["state"] == "waiting"
                            and successor_delivery is not None
                            and successor_delivery["state"] == "interrupt_waiting"
                        ):
                            if session_status != "active":
                                self._write_terminal_snapshot(
                                    conn,
                                    successor_turn_id,
                                    outcome="not_written",
                                    settled_by="session_archive",
                                    evidence_kind="unstarted_successor_retired",
                                )
                                retired = delivery_store.record_definitive_attempt(
                                    conn,
                                    successor_delivery_id,
                                    expected_version=int(successor_delivery["version"]),
                                    expected_states=("interrupt_waiting",),
                                    outcome="not_written",
                                    next_state="retired",
                                    receipt={"kind": "session_archive"},
                                )
                                if retired is None:
                                    raise RuntimeError("archived P0 successor retirement lost")
                            else:
                                started = delivery_store.cas_turn(
                                    conn,
                                    successor_turn_id,
                                    expected_version=int(successor["version"]),
                                    expected_states=("waiting",),
                                    values={"state": "starting"},
                                )
                                attempted = delivery_store.open_start_attempt(
                                    conn,
                                    successor_delivery_id,
                                    expected_version=int(successor_delivery["version"]),
                                    turn_id=successor_turn_id,
                                    attempt_id=delivery_store.new_attempt_id(),
                                )
                                if started is None or attempted is None:
                                    raise RuntimeError(
                                        "P0 successor claim lost after terminal proof"
                                    )
                                claimed_successor = successor_turn_id
                    latest_turn = delivery_store.get_turn(conn, turn_id)
                    if latest_turn is not None and latest_turn.get("control_state") in {
                        "pending",
                        "interrupting",
                        "waiting_terminal",
                        "reconciling",
                    }:
                        delivery_store.cas_turn(
                            conn,
                            turn_id,
                            expected_version=int(latest_turn["version"]),
                            expected_states=("terminal",),
                            values={
                                "control_state": "settled",
                                "control_receipt_outcome": (
                                    latest_turn.get("control_receipt_outcome") or "accepted"
                                ),
                            },
                        )
                    result = {
                        "changed": True,
                        "successor_turn_id": claimed_successor,
                        "delivery_id": materialized_id,
                        "preserve_queue": delivery_store.queue_is_held(conn, session_id),
                    }
        if materialized_id:
            self._publish_materialized_delivery(materialized_id)
        if result.get("changed"):
            self._publish_terminal_inbox_update(session_id)
        return result

    def _settle_durable_prewrite_failure(
        self,
        turn_id: str,
        *,
        outcome: str,
    ) -> dict[str, Any]:
        """Settle definitive no-write evidence at the terminal chokepoint."""
        return self._terminalize_durable_turn(
            turn_id,
            "not_written",
            settled_by=outcome,
            evidence_kind="definitive_prewrite_failure",
            evidence={"reason": outcome},
        )

    def _reconcile_durable_runner_release(
        self,
        turn_id: str,
        *,
        cancelled: bool,
        failed: bool,
        prewrite_refused: bool,
        settled_by: str | None,
        terminal_is_error: bool,
        hold_queue: bool,
    ) -> dict[str, Any]:
        """Contain post-native ownership writes so runner cleanup always finishes."""

        try:
            if cancelled:
                return self._terminalize_durable_turn(
                    turn_id,
                    "canceled",
                    settled_by=settled_by or SETTLED_BY_STOPPED,
                    evidence_kind="runner_release",
                    hold_queue=hold_queue,
                )
            if failed:
                # Dispatch may have written before raising. Preserve starting work
                # for exact-evidence recovery instead of replaying it.
                with self._sqlite_engine().begin() as conn:
                    reserve_write_lock(conn)
                    delivery = delivery_store.delivery_for_turn(conn, turn_id)
                    if delivery is not None and delivery["state"] == "start_attempting":
                        delivery_store.mark_attempt_unknown(
                            conn,
                            str(delivery["id"]),
                            expected_version=int(delivery["version"]),
                            receipt={"reason": "runner_dispatch_failure"},
                        )
                return {}
            if prewrite_refused:
                return self._settle_durable_prewrite_failure(
                    turn_id,
                    outcome=SETTLED_BY_REFUSED_CONCURRENT_TURN,
                )
            if settled_by is not None:
                return self._terminalize_durable_turn(
                    turn_id,
                    self._durable_terminal_outcome(
                        is_error=terminal_is_error,
                        settled_by=settled_by,
                    ),
                    settled_by=settled_by,
                    evidence_kind="runner_release",
                    hold_queue=hold_queue,
                )
        except Exception:
            logger.exception(
                "normal turn durable terminal reconciliation deferred for Turn=%s",
                turn_id,
            )
        return {}

    async def terminalize_turn(self, turn_id: str, *, outcome: str = "completed") -> bool:
        result = self._terminalize_durable_turn(
            turn_id,
            outcome,
            settled_by="explicit_terminalize",
            evidence_kind="explicit_terminalize",
        )
        successor_id = str(result.get("successor_turn_id") or "")
        if successor_id:
            await self._start_persisted_turn(successor_id)
        return bool(result.get("changed"))

    async def _run_pending_interrupt(
        self,
        session_id: str,
        logical_turn_id: str,
    ) -> None:
        with self._sqlite_engine().begin() as conn:
            reserve_write_lock(conn)
            pending = delivery_store.pending_control_for_turn(conn, logical_turn_id)
            if pending is None:
                return
            claimed = delivery_store.cas_turn(
                conn,
                logical_turn_id,
                expected_version=int(pending["version"]),
                expected_states=("active",),
                values={"control_state": "interrupting"},
            )
        if claimed is not None:
            await self._interrupt_durable_turn(session_id, logical_turn_id)

    async def _run_pending_steers(
        self,
        session_id: str,
        logical_turn_id: str,
        context: "MessageContext",
    ) -> None:
        while True:
            with self._sqlite_engine().begin() as conn:
                reserve_write_lock(conn)
                turn = delivery_store.get_turn(conn, logical_turn_id)
                if (
                    turn is None
                    or turn["session_id"] != session_id
                    or turn["state"] != "active"
                    or delivery_store.pending_control_for_turn(conn, logical_turn_id)
                    is not None
                ):
                    return
                native_turn_id = str(turn.get("native_turn_id") or "").strip()
                pending = delivery_store.pending_steer_for_turn(conn, logical_turn_id)
                if not native_turn_id or pending is None:
                    return
                attempt_id = delivery_store.new_attempt_id()
                claimed = delivery_store.open_steer_attempt(
                    conn,
                    str(pending["id"]),
                    expected_version=int(pending["version"]),
                    turn_id=logical_turn_id,
                    attempt_id=attempt_id,
                    expected_native_turn_id=native_turn_id,
                )
                if claimed is None:
                    continue
                steer_text = str(claimed.get("dispatch_text") or "")
                backend = str(turn["backend"])
                delivery_id = str(claimed["id"])
            receipt = await self._attempt_steer(
                backend,
                SteerRequest(
                    target_session_id=session_id,
                    expected_logical_turn_id=logical_turn_id,
                    expected_native_turn_id=native_turn_id,
                    text=steer_text,
                ),
            )
            await self._finish_steer(delivery_id, receipt, context=context)

    def _publish_materialized_delivery(self, delivery_id: str) -> None:
        """Publish one accepted immutable Message after its transaction commits."""

        try:
            with self._sqlite_engine().connect() as conn:
                row = messages_service.get_message(conn, delivery_id)
                inbox_row = (
                    messages_service.get_inbox_session(
                        conn,
                        str(row.get("session_id") or ""),
                        platform="avibe",
                    )
                    if row is not None and row.get("session_id")
                    else None
                )
            if row is None:
                return
            from core.inbox_events import bus

            bus.publish("message.new", row)
            bus.publish(
                "session.activity",
                {
                    "session_id": row.get("session_id"),
                    "scope_id": row.get("scope_id"),
                    "event": (
                        "show_event" if row.get("type") == messages_service.ANNOTATION_TYPE else "user_message"
                    ),
                },
            )
            if inbox_row is not None:
                bus.publish("inbox.session.updated", inbox_row)
        except Exception:
            logger.exception("accepted Delivery publish failed for %s", delivery_id)

    def _publish_terminal_inbox_update(self, session_id: str) -> None:
        """Publish the Inbox projection only after terminal ownership commits."""

        try:
            with self._sqlite_engine().connect() as conn:
                inbox_row = messages_service.get_inbox_session(
                    conn,
                    session_id,
                    platform="avibe",
                )
            if inbox_row is None:
                return
            from core.inbox_events import bus

            bus.publish("inbox.session.updated", inbox_row)
        except Exception:
            logger.debug(
                "terminal Inbox projection failed for Session=%s",
                session_id,
                exc_info=True,
            )

    def on_native_start(
        self,
        context: "MessageContext",
        *,
        backend: str,
        runtime_key: str,
        runtime_turn_id: str,
    ) -> asyncio.Task[None] | None:
        logical_turn_id = str(
            (getattr(context, "platform_specific", None) or {}).get("turn_token") or ""
        ).strip()
        session_id = self.controller._session_id_from_context(context) if self.controller else None
        if not logical_turn_id or not session_id:
            return
        if not self._durable_schema_available():
            return
        try:
            identity = self._active_identity(backend, session_id, logical_turn_id)
            native_turn_id = identity[1] if identity and identity[0] == logical_turn_id else None
            with self._sqlite_engine().begin() as conn:
                reserve_write_lock(conn)
                turn = delivery_store.get_turn(conn, logical_turn_id)
                if turn is None or turn["session_id"] != session_id:
                    return
                bound = delivery_store.bind_native_start(
                    conn,
                    logical_turn_id,
                    expected_version=int(turn["version"]),
                    runtime_key=runtime_key,
                    runtime_turn_id=runtime_turn_id,
                    native_turn_id=native_turn_id,
                )
                delivery = delivery_store.delivery_for_turn(conn, logical_turn_id)
                materialized = None
                if bound is not None and delivery is not None:
                    materialized = delivery_store.materialize_acceptance(
                        conn,
                        delivery_id=str(delivery["id"]),
                        expected_attempt_id=str(delivery.get("current_attempt_id") or "") or None,
                        accepted_turn_id=logical_turn_id,
                        evidence={
                            "kind": "native_start",
                            "runtime_key": runtime_key,
                            "runtime_turn_id": runtime_turn_id,
                            "native_turn_id": native_turn_id,
                        },
                    )
                    if materialized is None:
                        raise RuntimeError("native start could not materialize initial Delivery")
                has_pending_interrupt = bool(
                    bound is not None
                    and delivery_store.pending_control_for_turn(conn, logical_turn_id)
                )
                has_pending_steer = bool(
                    bound is not None
                    and native_turn_id
                    and not has_pending_interrupt
                    and delivery_store.pending_steer_for_turn(conn, logical_turn_id)
                )
        except Exception:
            logger.exception(
                "durable native start binding deferred to reconciliation for Turn=%s",
                logical_turn_id,
            )
            return None
        if materialized is not None:
            self._publish_materialized_delivery(str(materialized["id"]))
        if has_pending_interrupt:
            return asyncio.create_task(
                self._run_pending_interrupt(session_id, logical_turn_id),
                name=f"durable-interrupt:{session_id}",
            )
        if has_pending_steer:
            return asyncio.create_task(
                self._run_pending_steers(session_id, logical_turn_id, context),
                name=f"durable-steer:{session_id}",
            )
        return None

    async def _resume_after_native_terminal(
        self,
        session_id: str,
        logical_turn_id: str,
    ) -> None:
        try:
            current = self.in_flight.get(session_id)
            if current is not None and current.logical_turn_id == logical_turn_id:
                await asyncio.gather(current.task, return_exceptions=True)
            await self._resume_post_terminal(session_id)
        except Exception:
            logger.exception(
                "failed to resume durable Session after native terminal: %s",
                session_id,
            )

    def on_native_terminal(
        self,
        context: "MessageContext",
        *,
        outcome: str,
    ) -> asyncio.Task[None] | None:
        payload = getattr(context, "platform_specific", None) or {}
        logical_turn_id = str(payload.get("turn_token") or "").strip()
        runtime_turn_id = str(payload.get("agent_runtime_turn_token") or "").strip()
        if not logical_turn_id:
            return None
        if not self._durable_schema_available():
            return None
        if outcome == "terminal":
            get_sink = getattr(self.controller, "get_turn_sink", None)
            get_key = getattr(self.controller, "_get_session_key", None)
            if callable(get_sink) and callable(get_key):
                try:
                    sink = get_sink(get_key(context))
                except Exception:
                    logger.debug("failed to inspect native terminal Turn sink", exc_info=True)
                else:
                    if (
                        isinstance(sink, dict)
                        and sink.get("done_event") is not None
                        and str(sink.get("turn_token") or "") == logical_turn_id
                    ):
                        # The result hook captured this exact sink before output
                        # delivery. Its waiter owns the authoritative completed /
                        # failed / canceled outcome after sink settlement.
                        return None
        with self._sqlite_engine().connect() as conn:
            turn = delivery_store.get_turn(conn, logical_turn_id)
        if turn is None or turn["state"] not in delivery_store.TURN_OWNER_STATES:
            return None
        expected_runtime_id = str(turn.get("runtime_turn_id") or "").strip()
        if expected_runtime_id and runtime_turn_id != expected_runtime_id:
            return None
        session_id = str(turn["session_id"])
        semantic_outcome = outcome if outcome in {
            "completed",
            "failed",
            "canceled",
            "not_written",
        } else "completed"
        result = self._terminalize_durable_turn(
            logical_turn_id,
            semantic_outcome,
            settled_by="native_terminal",
            evidence_kind="native_terminal",
            evidence={"reported_outcome": outcome},
        )
        current = self.in_flight.get(session_id)
        should_resume = bool(result.get("successor_turn_id")) or (
            current is None and not bool(result.get("preserve_queue"))
        )
        if result.get("changed") and should_resume:
            return asyncio.create_task(
                self._resume_after_native_terminal(session_id, logical_turn_id),
                name=f"durable-terminal-resume:{session_id}",
            )
        return None

    async def recover_durable_delivery_state(self, session_id: str | None = None) -> list[str]:
        """Restore evidence, reconcile exact identities, then project status."""

        with self._sqlite_engine().connect() as conn:
            turns = delivery_store.recovery_turns(conn, session_id)
        pending_interrupts: list[tuple[str, str]] = []
        pending_steers: list[tuple[str, str]] = []
        recovered: list[str] = []
        materialized_ids: set[str] = set()
        for turn in turns:
            turn_id = str(turn["id"])
            target_session = str(turn["session_id"])
            with self._sqlite_engine().begin() as conn:
                reserve_write_lock(conn)
                latest = delivery_store.get_turn(conn, turn_id)
                terminal_stop_receipt = bool(
                    latest is not None
                    and latest["state"] in delivery_store.TURN_OWNER_STATES
                    and latest.get("control_receipt_outcome") == "not_active"
                )
            if terminal_stop_receipt:
                terminal = self._terminalize_durable_turn(
                    turn_id,
                    "canceled",
                    settled_by="adapter_not_active",
                    evidence_kind="stop_not_active",
                    evidence={"reason": "not_active", "source": "recovery"},
                    hold_queue=True,
                )
                recovered.append(target_session)
                projected = self.in_flight.get(target_session)
                if (
                    projected is not None
                    and projected.logical_turn_id == turn_id
                    and not projected.task.done()
                ):
                    projected.cancel_settled_by = "adapter_not_active"
                    projected.task.cancel()
                    await asyncio.gather(projected.task, return_exceptions=True)
                    if self.in_flight.get(target_session) is projected:
                        self.in_flight.pop(target_session, None)
                successor_turn_id = str(terminal.get("successor_turn_id") or "")
                if successor_turn_id:
                    await self._start_persisted_turn(successor_turn_id)
                continue
            identity = self._active_identity(
                str(turn["backend"]),
                target_session,
                turn_id,
            )
            with self._sqlite_engine().begin() as conn:
                reserve_write_lock(conn)
                latest = delivery_store.get_turn(conn, turn_id)
                if latest is None or latest["state"] not in delivery_store.TURN_OWNER_STATES:
                    continue
                if identity and identity[0] == turn_id:
                    bound = delivery_store.bind_native_start(
                        conn,
                        turn_id,
                        expected_version=int(latest["version"]),
                        runtime_key=latest.get("runtime_key"),
                        runtime_turn_id=latest.get("runtime_turn_id"),
                        native_turn_id=identity[1],
                    )
                    if bound is not None:
                        delivery = delivery_store.delivery_for_turn(conn, turn_id)
                        if delivery is not None and delivery["state"] in {
                            "start_attempting",
                            "reconciling_start",
                        }:
                            materialized = delivery_store.materialize_acceptance(
                                conn,
                                delivery_id=str(delivery["id"]),
                                expected_attempt_id=str(delivery.get("current_attempt_id") or "") or None,
                                accepted_turn_id=turn_id,
                                evidence={"kind": "restored_native_identity", "native_turn_id": identity[1]},
                            )
                            if materialized is not None:
                                materialized_ids.add(str(materialized["id"]))
                        recovered.append(target_session)
                        if delivery_store.pending_control_for_turn(conn, turn_id):
                            pending_interrupts.append((target_session, turn_id))
                        elif identity[1] and delivery_store.pending_steer_for_turn(
                            conn,
                            turn_id,
                        ):
                            pending_steers.append((target_session, turn_id))
                    continue
                delivery = delivery_store.delivery_for_turn(conn, turn_id)
                if delivery is None:
                    raise RuntimeError(f"durable Turn {turn_id} lost its initial Delivery")
                if delivery["state"] == "start_attempting":
                    delivery_store.mark_attempt_unknown(
                        conn,
                        str(delivery["id"]),
                        expected_version=int(delivery["version"]),
                        receipt={"reason": "restart_without_native_evidence"},
                    )

        with self._sqlite_engine().begin() as conn:
            reserve_write_lock(conn)
            for attempt in delivery_store.unresolved_deliveries(conn, session_id):
                if attempt["state"] == "steering":
                    delivery_store.mark_attempt_unknown(
                        conn,
                        str(attempt["id"]),
                        expected_version=int(attempt["version"]),
                        receipt={"reason": "restart_after_steer_call"},
                    )

        for target_session, turn_id in pending_interrupts:
            await self._run_pending_interrupt(target_session, turn_id)
        for target_session, turn_id in pending_steers:
            await self._run_pending_steers(
                target_session,
                turn_id,
                self._delivery_context(target_session),
            )
        # Reservations are definitive pre-write admissions left between the UI /
        # Harness transaction and controller claim. Re-enter the same manager path
        # after adapter restoration; its writer CAS makes concurrent recovery and
        # a late original dispatch idempotent.
        with self._sqlite_engine().connect() as conn:
            reservations = delivery_store.recoverable_reservations(conn, session_id)
        for reservation in reservations:
            target_session = str(reservation["session_id"])
            try:
                context = self._delivery_context(target_session)
                self._hydrate_delivery_context(context, reservation)
                result = await self.deliver(
                    self._request_from_delivery(reservation),
                    context=context,
                )
            except Exception:
                logger.exception(
                    "failed to recover reserved Delivery=%s",
                    reservation["id"],
                )
            else:
                if result.state != "reserved":
                    recovered.append(target_session)
        with self._sqlite_engine().begin() as conn:
            reserve_write_lock(conn)
            queued_without_owner = set(
                delivery_store.queued_session_ids_without_live_turns(conn, session_id)
            )
        for target_session in sorted(queued_without_owner):
            await self._resume_post_terminal(target_session)
        for delivery_id in sorted(materialized_ids):
            self._publish_materialized_delivery(delivery_id)
        self.project_durable_agent_status()
        return sorted(set(recovered))

    def project_durable_agent_status(self) -> None:
        with self._sqlite_engine().begin() as conn:
            reserve_write_lock(conn)
            live = delivery_store.session_ids_with_live_turns(conn)
            owned = delivery_store.session_ids_with_turn_history(conn)
            idle = owned - live
            if idle:
                cleared = conn.execute(
                    update(agent_sessions)
                    .where(agent_sessions.c.id.in_(idle))
                    .where(agent_sessions.c.status == "active")
                    .where(agent_sessions.c.agent_status == "running")
                    .values(agent_status="idle")
                )
                _ = cleared.rowcount
            if live:
                running = conn.execute(
                    update(agent_sessions)
                    .where(agent_sessions.c.id.in_(live))
                    .where(agent_sessions.c.status == "active")
                    .where(agent_sessions.c.agent_status != "running")
                    .values(agent_status="running")
                )
                _ = running.rowcount

    async def _resume_durable_session(self, session_id: str) -> None:
        with self._sqlite_engine().connect() as conn:
            owner = delivery_store.active_turn(conn, session_id)
        if owner is None:
            await self.drain_delivery_queue(session_id)
        elif owner["state"] == "starting":
            await self._start_persisted_turn(str(owner["id"]))

    async def _resume_post_terminal(self, session_id: str) -> None:
        with self._sqlite_engine().begin() as conn:
            reserve_write_lock(conn)
            owner = delivery_store.active_turn(conn, session_id)
        if owner is not None:
            await self._resume_durable_session(session_id)
        else:
            await self.drain_delivery_queue(session_id)

    async def submit(
        self,
        session_id: Optional[str],
        context: "MessageContext",
        text: str,
        *,
        source: str = SOURCE_HUMAN,
        delivery_intent: Literal["queue", "send_now"] = "queue",
    ) -> TurnSubmissionResult:
        """Admit one caller through the durable Delivery owner."""
        if delivery_intent not in {"queue", "send_now"}:
            raise ValueError(f"unsupported delivery intent: {delivery_intent}")
        if not (isinstance(session_id, str) and session_id):
            # No session key (CLI-style) — just run; nothing to queue against.
            await self._run(None, context, text, source=source)
            return TurnSubmissionResult(
                route="ran",
                delivery_status="ran" if delivery_intent == "send_now" else None,
            )

        spec = dict(getattr(context, "platform_specific", None) or {})
        with self._sqlite_engine().connect() as conn:
            busy = delivery_store.active_turn(conn, session_id) is not None
        source_value = "harness" if source == SOURCE_SCHEDULED else "user"
        delivery_id = str(spec.get("delivery_id") or "").strip() or None
        native_message_id = str(spec.get("native_message_id") or "").strip() or None
        if native_message_id is None and source == SOURCE_SCHEDULED:
            native_message_id = str(getattr(context, "message_id", None) or "").strip() or None
        if delivery_id is None:
            candidate = str(getattr(context, "message_id", None) or "").strip()
            if candidate.startswith("msg_"):
                delivery_id = candidate
        request = DeliveryRequest(
            session_id=session_id,
            priority="p0" if delivery_intent == "send_now" else "p3",
            content=text,
            has_content=bool(text or getattr(context, "files", None)),
            delivery_id=delivery_id,
            scope_id=str(spec.get("scope_id") or "").strip() or None,
            platform="avibe",
            source=source_value,
            author="harness" if source == SOURCE_SCHEDULED else "user",
            message_type="harness" if source == SOURCE_SCHEDULED else "user",
            display_text=str(spec.get("display_text") or text),
            content_json=spec.get("message_content") if isinstance(spec.get("message_content"), dict) else None,
            metadata=spec.get("message_metadata") if isinstance(spec.get("message_metadata"), dict) else {},
            author_id=str(spec.get("author_id") or "").strip() or None,
            author_name=str(spec.get("author_name") or "").strip() or None,
            native_message_id=native_message_id,
        )
        result = await self.deliver(request, context=context)
        enqueued_states = {"queued", "pending_steer", "steering", "reconciling_start", "reconciling_steer", "interrupt_waiting"}
        enqueued = result.state in enqueued_states
        if enqueued:
            from core.inbox_events import bus

            bus.publish("queue.updated", {"session_id": session_id})
        return TurnSubmissionResult(
            route="enqueued" if enqueued else "ran",
            queue_persisted=True,
            target_was_busy=busy,
            delivery_status=result.state if delivery_intent == "send_now" else None,
        )

    async def _run(
        self,
        session_id: Optional[str],
        context: "MessageContext",
        text: str,
        *,
        source: str = SOURCE_HUMAN,
        logical_turn_id: str | None = None,
        delivery_id: str | None = None,
        durable_preallocated: bool = False,
    ) -> None:
        """Start a fire-and-forget turn and HOLD it open until it settles.

        A no-op chunk sink keeps ``dispatch_turn`` alive for the turn's lifetime so
        ``in_flight`` stays populated (Stop works) and the session-level
        ``turn.start`` / ``turn.end`` lifecycle is published for the browser's
        working indicator. On NATURAL completion the queue is flushed: messages the
        user sent while this turn ran are merged + run as the next turn. A user Stop
        (cancellation) does NOT flush — the queue is kept per the user's "don't
        clear the queue on stop" rule — unless ``send-now`` opted this session into
        ``flush_on_cancel``. The reply reaches the browser over ``message.new``.

        ``source`` selects the human vs. scheduler turn path in ``dispatch_turn``;
        a scheduled / watch run passes ``SOURCE_SCHEDULED`` so it goes through the
        SAME gate (in_flight + turn.start/turn.end + queue draining) as a Chat turn.
        There is NO turn-duration timeout: a long agent runs for hours and is freed
        only by a real terminal signal (Phase 1a — STUCK/sentinel removed).
        """
        from core.inbox_events import bus

        durable_turn_registered = durable_preallocated
        if durable_preallocated and not logical_turn_id:
            raise ValueError("preallocated durable Turn requires its logical id")
        if context.platform_specific is None:
            context.platform_specific = {}
        context_turn_id = str(context.platform_specific.get("turn_token") or "").strip()
        if isinstance(session_id, str) and session_id:
            if self._durable_schema_available() and not durable_preallocated:
                raise RuntimeError("Session native dispatch requires a preallocated Delivery Turn")
            logical_turn_id = logical_turn_id or context_turn_id or uuid.uuid4().hex
        else:
            logical_turn_id = logical_turn_id or context_turn_id or uuid.uuid4().hex
        context.platform_specific["turn_token"] = logical_turn_id

        async def _runner() -> None:
            cancelled = False
            failed = False
            # How this turn's waiter was released, in the ``core.run_settlement``
            # vocabulary. Anything other than a real terminal result means no result
            # is coming, so an ``agent_runs`` row this turn owns has to be settled
            # here — the gate lane returns to ``_execute_agent_run`` long before the
            # turn ends, so nobody downstream can do it (Codex P1).
            settled_by: Optional[str] = None
            try:
                outcome = await dispatch_turn_with_outcome(
                    self.controller,
                    context,
                    text,
                    source=source,
                    # ALWAYS pass the no-op sink — even for scheduled runs. It isn't
                    # about the browser (chunks are discarded; avibe renders from
                    # message.new); it makes ``dispatch_turn`` HOLD the turn open
                    # until the backend's terminal result, keeping ``in_flight``
                    # populated for the turn's whole lifetime. With ``on_chunk=None``
                    # an async backend (Codex/Claude) returns at prompt-submit, so the
                    # slot would free + a Chat send could preempt the still-running
                    # scheduled turn (Codex P2).
                    on_chunk=self._noop_chunk,
                )
                settled_by = outcome.settled_by
            except asyncio.CancelledError:
                cancelled = True
                # Do NOT decide the reason here: the canceller knows it, and it is
                # recorded on the Turn (``cancel_settled_by``) which is only popped
                # in the ``finally`` below. A plain Stop leaves it unset and reads
                # as ``SETTLED_BY_STOPPED``; a backend runtime refresh sets its own
                # value so it is not misreported as a user stop (Codex P1).
                raise
            except Exception:
                # dispatch_turn raised before any backend turn was actually
                # dispatched (missing/disabled backend, synchronous setup error).
                # No agent reply was produced, so this is a terminal FAILURE — it must
                # NOT auto-flush the send-while-busy queue onto a fresh turn (Codex
                # P2). (An explicit send-now flush_on_cancel still flushes.)
                failed = True
                settled_by = SETTLED_BY_NO_TERMINAL_RESULT
                logger.exception("internal async dispatch failed for session=%s", session_id)
            finally:
                if isinstance(session_id, str):
                    durable_terminal_result: dict[str, Any] = {}
                    prewrite_refused = (
                        settled_by == SETTLED_BY_REFUSED_CONCURRENT_TURN
                    )
                    # The turn is over — the agent emitted its terminal result, the
                    # user stopped it, or dispatch raised before any backend turn.
                    # NO turn-duration timeout: the slot is freed only by a real
                    # terminal signal here (Phase 1a — STUCK/sentinel removed).
                    current = self.in_flight.get(session_id)
                    turn = (
                        current
                        if current is not None
                        and current.task is asyncio.current_task()
                        and current.logical_turn_id == logical_turn_id
                        else None
                    )
                    terminal_is_error = bool(
                        turn is not None and turn.terminal_is_error
                    )
                    if turn is not None:
                        self.in_flight.pop(session_id, None)
                    if turn is not None:
                        bus.publish("turn.end", {"session_id": session_id})
                    if cancelled:
                        # Attribute the cancellation to whoever caused it. The Turn
                        # carries the cause when the canceller had a more specific one
                        # than "the user stopped this"; a plain Stop / send-now leaves
                        # it unset, and ``stopped`` (→ ``canceled``) stays the default
                        # reading of a cancelled turn.
                        settled_by = (
                            getattr(turn, "cancel_settled_by", None) if turn is not None else None
                        ) or SETTLED_BY_STOPPED
                    self._settle_model_hub_turn(context, settled_by)
                    if logical_turn_id and durable_turn_registered:
                        hold_queue = bool(
                            settled_by == SETTLED_BY_STOPPED
                            and turn is not None
                            and turn.stop_no_flush
                            and not turn.flush_on_cancel
                        )
                        durable_terminal_result = self._reconcile_durable_runner_release(
                            logical_turn_id,
                            cancelled=cancelled,
                            failed=failed,
                            prewrite_refused=prewrite_refused,
                            settled_by=settled_by,
                            terminal_is_error=terminal_is_error,
                            hold_queue=hold_queue,
                        )
                    # Converge the no-terminal-result outcome onto the OUTBOUND status
                    # chokepoint. The normal path already emitted a terminal result;
                    # only ``failed`` reaches here without one: dispatch raised before
                    # any backend turn (missing/disabled backend) → empty error result
                    # → dot red. This is a real terminal FAILURE, not a timeout.
                    if failed:
                        await self.controller.emit_agent_message(
                            context,
                            "result",
                            "",
                            is_error=True,
                            output=terminal_turn_output(),
                        )
                    # Settle before flushing: the next turn must not start while a run
                    # this one owned is still ``running``. Placed after the failure
                    # emit above so the honest outbound terminal writes first and this
                    # guarded write degrades to a no-op.
                    self._settle_turn_owned_agent_runs(context, settled_by)
                    # Flush intents ride on the popped Turn (set by cancel / send_now),
                    # so they retire with it — no parallel set to discard. Don't flush
                    # after a plain Stop (keep the queue) or a terminal failure; send-now
                    # still forces a flush via flush_on_cancel.
                    preserve_durable_queue = bool(
                        durable_turn_registered
                        and durable_terminal_result.get("preserve_queue")
                    )
                    should_flush = not preserve_durable_queue and (
                        (
                            not cancelled
                            and not failed
                            and not prewrite_refused
                            and not (turn is not None and turn.stop_no_flush)
                        )
                        or (turn is not None and turn.flush_on_cancel)
                    )
                    backend = self._context_backend(context)
                    if should_flush and backend in self._draining_backends:
                        self._deferred_restart_sessions.setdefault(backend, set()).add(session_id)
                    elif should_flush:
                        if durable_turn_registered:
                            await self._resume_post_terminal(session_id)
                        else:
                            await self.flush_queue(session_id)
                    elif durable_turn_registered and durable_terminal_result.get(
                        "successor_turn_id"
                    ):
                        await self._resume_durable_session(session_id)

        task = asyncio.create_task(_runner(), name="internal-dispatch-async")
        if isinstance(session_id, str) and session_id:
            self.in_flight[session_id] = Turn(
                task=task,
                context=context,
                started_at=_utc_now_iso(),
                logical_turn_id=logical_turn_id,
                delivery_id=delivery_id,
            )
            # Make the DB row authoritative at ACCEPTANCE, not at dispatch start:
            # ``update_session``'s backend lock re-checks ``agent_status`` inside
            # its UPDATE predicate, so writing ``running`` synchronously here —
            # before the loop can start the dispatch task — closes the startup
            # window where a cross-backend PATCH could land while the row still
            # read idle (and would then be silently undone by the bind-time
            # backfill). The inbound chokepoint's own ``running`` write becomes a
            # no-op; every terminal path still settles the status (outbound
            # chokepoint / cancel / startup recovery).
            self.controller.set_agent_status(session_id, "running")
            bus.publish("turn.start", {"session_id": session_id})

    async def flush_queue(self, session_id: str) -> bool:
        """Drain one claimable Delivery through the sole FIFO owner."""
        return await self.drain_delivery_queue(session_id)

    async def recover_persisted_agent_run_queue(
        self,
        session_id: Optional[str] = None,
    ) -> list[str]:
        """Resume durable Workbench Agent Run queues after their owner vanished.

        ``workbench_queue_holds_run`` rows are deliberately invisible to the
        scheduler because the Session FSM owns their FIFO position. A process
        restart drops the in-memory owner and therefore must re-enter that FSM.
        Recovery is evidence-based: only a persisted queue row that references
        a still-queued Agent Run is eligible. An older, scheduler-owned queued
        Run defers recovery until that Run reaches its normal synchronous or
        terminal path, preserving FIFO across restart.
        """

        if self._build_context is None:
            return []
        with self._sqlite_engine().connect() as conn:
            session_ids = delivery_store.queued_session_ids_without_live_turns(
                conn,
                session_id,
            )
        recovered: list[str] = []
        for queued_session_id in session_ids:
            lock = self._queue_recovery_locks.setdefault(queued_session_id, asyncio.Lock())
            async with lock:
                with self._sqlite_engine().connect() as conn:
                    head = delivery_store.claimable_fifo_head(conn, queued_session_id)
                    if head is None:
                        continue
                    head_payload = delivery_store.delivery_payload(head)
                    provenance = (head_payload.get("metadata") or {}).get(
                        SCHEDULED_PROVENANCE_KEY
                    )
                    spec = (
                        provenance.get("platform_specific")
                        if isinstance(provenance, dict)
                        else None
                    )
                    run_id = delivery_store.owned_agent_run_id(head)
                    if (
                        not isinstance(spec, dict)
                        or spec.get("task_trigger_kind") != "agent_run"
                        or not run_id
                    ):
                        continue
                    run_rows = list(
                        conn.execute(
                            select(
                                agent_runs.c.id,
                                agent_runs.c.created_at,
                                agent_runs.c.status,
                                agent_runs.c.metadata_json,
                            )
                            .where(agent_runs.c.session_id == queued_session_id)
                            .where(agent_runs.c.run_type == "agent_run")
                            .order_by(agent_runs.c.created_at, agent_runs.c.id)
                        ).mappings()
                    )
                live_reference = next(
                    (
                        row
                        for row in run_rows
                        if str(row["id"]) == run_id
                        and normalize_run_status(row["status"]) == "queued"
                        and _run_metadata_holds_delivery(row["metadata_json"])
                    ),
                    None,
                )
                if live_reference is None:
                    continue
                reference_order = (
                    str(live_reference.get("created_at") or ""),
                    str(live_reference["id"]),
                )
                if any(
                    normalize_run_status(row["status"]) == "queued"
                    and str(row["id"]) != run_id
                    and not _run_metadata_holds_delivery(row["metadata_json"])
                    and (str(row.get("created_at") or ""), str(row["id"]))
                    <= reference_order
                    for row in run_rows
                ):
                    continue
                if await self.drain_delivery_queue(queued_session_id):
                    recovered.append(queued_session_id)
        return recovered

    def turn_state(self, session_id: str) -> dict:
        """Compose orthogonal foreground, inbox, Activity, and connection facts."""
        entry = self.in_flight.get(session_id)
        active = entry is not None and not entry.task.done()
        native_turn_started = False
        backend = ""
        backend_alive: Optional[bool] = None
        owner: dict[str, Any] | None = None
        if active and entry is not None:
            payload = getattr(entry.context, "platform_specific", None) or {}
            target = payload.get("agent_session_target")
            if isinstance(target, dict):
                backend = str(target.get("agent_backend") or "").strip()
            service = getattr(self.controller, "agent_service", None) if self.controller is not None else None
            started = getattr(service, "runtime_turn_started", None)
            if callable(started):
                native_turn_started = started(entry.context) is True
            probe = getattr(self.controller, "backend_alive", None) if self.controller is not None else None
            if native_turn_started and callable(probe):
                try:
                    backend_alive = probe(entry.context)
                except Exception:
                    logger.debug("turn_state: backend liveness probe failed", exc_info=True)
            coalesced = payload.get("coalesced_queue")
            owner_run_ids = (
                [str(value) for value in coalesced.get("execution_ids", []) if str(value or "").strip()]
                if isinstance(coalesced, dict) and isinstance(coalesced.get("execution_ids"), list)
                else []
            )
            owner_run_id = str(payload.get("task_execution_id") or "").strip()
            if owner_run_id and owner_run_id not in owner_run_ids:
                owner_run_ids.insert(0, owner_run_id)
            owner = {
                "source": str(payload.get("task_trigger_kind") or payload.get("turn_source") or "human"),
                "acquired_at": entry.started_at or None,
                "run_id": owner_run_id or None,
                "run_ids": owner_run_ids,
                "runtime_key": str(payload.get("agent_runtime_turn_key") or "").strip() or None,
                "native_turn_started": native_turn_started,
                "backend_alive": backend_alive,
            }
        pending_input_count = 0
        harness_activities: list[dict[str, Any]] = []
        try:
            with self._sqlite_engine().begin() as conn:
                pending_input_count = len(delivery_store.list_queued(conn, session_id))
                if not active and self._durable_schema_available():
                    durable_turn = delivery_store.active_turn(conn, session_id)
                    if durable_turn is not None:
                        active = True
                        native_turn_started = durable_turn["state"] == "active"
                        backend = str(durable_turn.get("backend") or "").strip()
                        initial = delivery_store.delivery_for_turn(
                            conn,
                            str(durable_turn["id"]),
                        )
                        record: dict[str, Any] = {}
                        if initial is not None:
                            record = delivery_store.delivery_payload(initial)
                            if initial.get("message_id"):
                                message = messages_service.get_message(
                                    conn,
                                    str(initial["message_id"]),
                                    session_id=session_id,
                                )
                                if message is not None:
                                    record = message
                        metadata = record.get("metadata") or {}
                        provenance = metadata.get(SCHEDULED_PROVENANCE_KEY)
                        restored_spec = (
                            provenance.get("platform_specific")
                            if isinstance(provenance, dict)
                            and isinstance(provenance.get("platform_specific"), dict)
                            else {}
                        )
                        owner_run_ids = sorted(self._agent_run_ids_from_spec(restored_spec))
                        native_message_id = str(record.get("native_message_id") or "")
                        if native_message_id.startswith("agent_run:"):
                            native_run_id = native_message_id.removeprefix("agent_run:")
                            if native_run_id and native_run_id not in owner_run_ids:
                                owner_run_ids.insert(0, native_run_id)
                        owner_run_id = str(restored_spec.get("task_execution_id") or "").strip()
                        if not owner_run_id and owner_run_ids:
                            owner_run_id = owner_run_ids[0]
                        owner = {
                            "source": str(
                                restored_spec.get("task_trigger_kind")
                                or restored_spec.get("turn_source")
                                or ("scheduled" if isinstance(provenance, dict) else "human")
                            ),
                            "acquired_at": durable_turn.get("started_at")
                            or durable_turn.get("created_at"),
                            "run_id": owner_run_id or None,
                            "run_ids": owner_run_ids,
                            "runtime_key": str(durable_turn.get("runtime_key") or "").strip()
                            or None,
                            "native_turn_started": native_turn_started,
                            "backend_alive": None,
                        }
                try:
                    harness_activities = derive_session_harness_activities(conn, session_id)
                except Exception:
                    logger.debug("turn_state: failed to derive harness activities", exc_info=True)
        except Exception:
            logger.debug("turn_state: failed to read queued input count", exc_info=True)
        activity_state: dict[str, Any] = {
            "background_activities": [],
            "pending_activity_output_count": 0,
            "connection": "unknown",
        }
        service = getattr(self.controller, "agent_service", None) if self.controller is not None else None
        registry = getattr(service, "activities", None)
        project = getattr(registry, "session_state", None)
        if callable(project):
            try:
                activity_state = project(session_id)
            except Exception:
                logger.debug("turn_state: failed to project Activity state", exc_info=True)
        # Unified background-work banner: process-local backend activities from the
        # registry, then live-derived harness items from the durable store. The
        # registry is never mutated — harness items are appended only to this
        # projection so the banner survives restarts correct-by-construction.
        background_activities = [
            _as_backend_activity_item(item)
            for item in activity_state.get("background_activities", [])
            if isinstance(item, dict)
        ]
        background_activities.extend(harness_activities)
        result = {
            "ok": True,
            "session_id": session_id,
            "in_flight": active,
            "native_turn_started": native_turn_started,
            "foreground": "running" if active else "idle",
            "pending_input_count": pending_input_count,
            "background_activities": background_activities,
            "pending_activity_output_count": activity_state.get(
                "pending_activity_output_count",
                0,
            ),
            "connection": activity_state.get("connection", "unknown"),
        }
        if backend:
            result["backend"] = backend
        if owner is not None:
            result["owner"] = owner
        return result

    async def release_for_backend_refresh(
        self,
        *,
        backend: str,
        base_session_ids: set[str],
    ) -> int:
        """Release active Workbench turns whose backend runtime is being refreshed.

        A backend refresh is a terminal runtime event: Codex/OpenCode/Claude cached
        process state can disappear underneath a Workbench turn before that turn's
        normal result path emits ``turn.end``. The manager owns the Workbench gate,
        so it must explicitly retire matching in-flight turns before the backend
        adapter clears its private registry. Otherwise Stop keeps targeting a turn
        id that no longer exists in the backend.
        """
        if not backend or not base_session_ids:
            return 0

        released_sessions: set[str] = set()
        tasks_to_settle: list[asyncio.Task] = []
        for session_id, turn in list(self.in_flight.items()):
            if session_id not in base_session_ids:
                continue
            spec = getattr(turn.context, "platform_specific", None) or {}
            target = spec.get("agent_session_target")
            turn_backend = (
                str(target.get("agent_backend") or "").strip()
                if isinstance(target, dict)
                else ""
            )
            if turn_backend and turn_backend != backend:
                continue
            turn.stop_no_flush = True
            # Record the cause BEFORE cancelling: this is a runtime refresh, not a
            # user Stop, so a scheduled run this turn owns must not settle as
            # ``canceled`` with the user-stop explanation (Codex P1). ``_run`` reads
            # it off the Turn when it pops it.
            turn.cancel_settled_by = SETTLED_BY_BACKEND_REFRESH
            if turn.task.done():
                self.in_flight.pop(session_id, None)
                from core.inbox_events import bus

                bus.publish("turn.end", {"session_id": session_id})
            else:
                turn.task.cancel()
                tasks_to_settle.append(turn.task)
            if self.controller is not None:
                self.controller.set_agent_status(session_id, "idle")
            if backend in self._draining_backends:
                self._deferred_restart_sessions.setdefault(backend, set()).add(session_id)
            released_sessions.add(session_id)
        if tasks_to_settle:
            await asyncio.gather(*tasks_to_settle, return_exceptions=True)
        released_restored: set[str] = set()
        restored_owners: list[dict[str, Any]] = []
        if self._durable_schema_available():
            with self._sqlite_engine().connect() as conn:
                restored_owners = delivery_store.live_turns_for_backend_sessions(
                    conn,
                    backend,
                    base_session_ids,
                )
        for owner in restored_owners:
            terminal = self._terminalize_durable_turn(
                str(owner["id"]),
                "canceled",
                settled_by=SETTLED_BY_BACKEND_REFRESH,
                evidence_kind="backend_refresh",
            )
            if terminal.get("changed"):
                released_restored.add(str(owner["session_id"]))
        for session_id in released_restored:
            if self.controller is not None:
                self.controller.set_agent_status(session_id, "idle")
            if backend in self._draining_backends:
                self._deferred_restart_sessions.setdefault(backend, set()).add(session_id)
        released_sessions.update(released_restored)
        released = len(released_sessions)
        if released:
            logger.info(
                "Released %d active Workbench turn(s) for %s runtime refresh",
                released,
                backend,
            )
        return released

    async def cancel(self, session_id: str) -> dict:
        """Persist one empty-P0 control request against the exact active Turn."""
        turn = self.in_flight.get(session_id)
        if not self._durable_schema_available():
            return await self._cancel_legacy_turn(session_id, turn)
        with self._sqlite_engine().connect() as conn:
            owner = delivery_store.active_turn(conn, session_id)
        if owner is None:
            return {"ok": False, "code": "not_in_flight", "session_id": session_id}
        result = await self.deliver(
            DeliveryRequest(session_id=session_id, priority="p0", content=None),
            context=turn.context if turn is not None else None,
        )
        if result.state in {"waiting_terminal", "interrupt_waiting"}:
            return {"ok": True, "session_id": session_id, "status": "cancel_requested"}
        if result.state == "settled" and result.reason == "not_active":
            return {
                "ok": True,
                "session_id": session_id,
                "status": "stale_released",
                "reason": "not_active",
            }
        return {
            "ok": False,
            "code": "stop_unknown" if result.state == "reconciling" else "stop_failed",
            "session_id": session_id,
            "reason": result.reason,
        }

    async def _cancel_legacy_turn(self, session_id: str, turn: Turn | None) -> dict:
        """Keep non-Workbench/test runtimes on the existing in-memory Stop path."""
        if turn is None:
            return {"ok": False, "code": "not_in_flight", "session_id": session_id}
        if turn.task.done():
            return {"ok": True, "session_id": session_id, "status": "already_finished"}
        turn.stop_no_flush = True
        if turn.context.platform_specific is None:
            turn.context.platform_specific = {}
        turn.context.platform_specific["suppress_stop_no_active_notice"] = True
        stopped = False
        try:
            stopped = bool(await self.controller.command_handler.handle_stop(turn.context))
        except Exception:
            logger.exception("internal cancel: backend stop failed for session=%s", session_id)
        if not stopped:
            spec = getattr(turn.context, "platform_specific", None) or {}
            reason = str(spec.get("stop_failure_reason") or "").strip()
            if reason in {"not_active", "runtime_unavailable"}:
                turn.task.cancel()
                await asyncio.gather(turn.task, return_exceptions=True)
                released_turn = self.in_flight.pop(session_id, None)
                from core.inbox_events import bus

                if released_turn is not None:
                    bus.publish("turn.end", {"session_id": session_id})
                if self.controller is not None:
                    self.controller.set_agent_status(session_id, "idle")
                backend = self._context_backend(turn.context)
                deferred = self._deferred_restart_sessions.get(backend)
                if deferred is not None:
                    deferred.discard(session_id)
                return {
                    "ok": True,
                    "session_id": session_id,
                    "status": "stale_released",
                    "reason": reason,
                }
            turn.stop_no_flush = False
            return {
                "ok": False,
                "code": "stop_failed",
                "session_id": session_id,
                "reason": reason or None,
            }
        backend = self._context_backend(turn.context)
        deferred = self._deferred_restart_sessions.get(backend)
        if deferred is not None:
            deferred.discard(session_id)
        turn.task.cancel()
        return {"ok": True, "session_id": session_id, "status": "cancel_requested"}

    @staticmethod
    def _clear_send_now_task(
        turn: Turn,
        task: asyncio.Task[dict[str, Any]],
    ) -> None:
        if turn.send_now_task is task:
            turn.send_now_task = None

    async def _interrupt_for_send_now(self, session_id: str, turn: Turn) -> dict:
        """Own one backend Stop attempt shared by concurrent send-now callers."""

        turn.flush_on_cancel = True
        turn.cancel_settled_by = SETTLED_BY_STOPPED
        if turn.context.platform_specific is None:
            turn.context.platform_specific = {}
        turn.context.platform_specific["suppress_stop_no_active_notice"] = True
        stopped = False
        try:
            stopped = bool(
                await self.controller.command_handler.handle_stop(turn.context)
            )
        except Exception:
            logger.exception(
                "internal send-now: backend stop failed for session=%s",
                session_id,
            )
        if not stopped:
            turn.flush_on_cancel = False
            turn.cancel_settled_by = None
            return {
                "ok": False,
                "code": "stop_failed",
                "session_id": session_id,
            }
        turn.task.cancel()
        return {
            "ok": True,
            "session_id": session_id,
            "status": "interrupted",
        }

    async def send_now(self, session_id: str) -> dict:
        """Promote the exact Delivery ordering head through empty P1."""
        with self._sqlite_engine().connect() as conn:
            head = delivery_store.ordering_head(conn, session_id)
        if head is None:
            return {"ok": True, "session_id": session_id, "status": "empty"}
        result = await self.deliver(
            DeliveryRequest(session_id=session_id, priority="p1", content=None),
        )
        if result.state == "refused":
            return {
                "ok": False,
                "code": result.reason or "send_now_refused",
                "session_id": session_id,
            }
        return {
            "ok": True,
            "session_id": session_id,
            "status": result.state,
            "delivery_id": result.delivery_id,
        }

    # --- shared turn chokepoints (status + Show checkpoint projection) ------------

    def _begin_show_checkpoint(self, context: "MessageContext") -> None:
        service = getattr(self.controller, "show_git_checkpoint_service", None)
        begin_turn = getattr(service, "begin_turn", None)
        if not callable(begin_turn):
            return
        try:
            begin_turn(self.controller, context)
        except Exception:
            logger.exception("Show checkpoint start hook failed")

    def _end_show_checkpoint(self, context: "MessageContext") -> None:
        service = getattr(self.controller, "show_git_checkpoint_service", None)
        end_turn = getattr(service, "end_turn", None)
        if not callable(end_turn):
            return
        try:
            end_turn(context)
        except Exception:
            logger.exception("Show checkpoint end hook failed")

    @staticmethod
    def _set_context_flag(context: "MessageContext", key: str, value: bool) -> None:
        payload = dict(getattr(context, "platform_specific", None) or {})
        if value:
            payload[key] = True
        else:
            payload.pop(key, None)
        context.platform_specific = payload

    @staticmethod
    def _pop_context_flag(context: "MessageContext", key: str) -> bool:
        payload = dict(getattr(context, "platform_specific", None) or {})
        value = bool(payload.pop(key, False))
        context.platform_specific = payload
        return value

    def _agent_initiated_turn_will_register(self, context: "MessageContext") -> bool:
        service = getattr(self.controller, "agent_service", None)
        runtime_started = getattr(service, "runtime_turn_started", None)
        if not callable(runtime_started) or runtime_started(context) is not True:
            return False
        session_id = self.controller._session_id_from_context(context)
        get_key = getattr(self.controller, "_get_session_key", None)
        if not session_id or not callable(get_key):
            return False
        session_key = get_key(context)
        return session_id not in self.in_flight and self.get_turn_sink(session_key) is None

    def on_running(self, context: "MessageContext") -> None:
        """INBOUND turn chokepoint shared by every source and backend."""
        if self.controller is None:
            return
        if self._agent_initiated_turn_will_register(context):
            # Agent-initiated turns register their FSM bus lifecycle immediately
            # after on_running. Let that start publish first so checkpointing
            # observes path ownership and never emits a duplicate pair.
            self._set_context_flag(context, _SHOW_CHECKPOINT_DEFERRED_START_KEY, True)
        else:
            self._begin_show_checkpoint(context)
        session_id = self.controller._session_id_from_context(context)
        if session_id:
            self.controller.set_agent_status(session_id, "running")

    @staticmethod
    def _durable_terminal_outcome(*, is_error: bool, settled_by: str | None) -> str:
        if is_error:
            return "failed"
        if settled_by == SETTLED_BY_STOPPED:
            return "canceled"
        return "completed"

    def _finish_durable_terminal_result(
        self,
        session_id: str,
        logical_turn_id: str,
        *,
        is_error: bool,
        settled_by: str | None,
    ) -> None:
        current = self.in_flight.get(session_id)
        hold_queue = bool(
            settled_by == SETTLED_BY_STOPPED
            and current is not None
            and current.logical_turn_id == logical_turn_id
            and current.stop_no_flush
            and not current.flush_on_cancel
        )
        try:
            terminal = self._terminalize_durable_turn(
                logical_turn_id,
                self._durable_terminal_outcome(
                    is_error=is_error,
                    settled_by=settled_by,
                ),
                settled_by=settled_by or SETTLED_BY_TERMINAL_RESULT,
                evidence_kind="terminal_result",
                hold_queue=hold_queue,
            )
        except Exception:
            logger.exception(
                "durable terminal reconciliation deferred after native completion for Turn=%s",
                logical_turn_id,
            )
            return
        current = self.in_flight.get(session_id)
        should_resume = bool(terminal.get("successor_turn_id")) or not bool(
            terminal.get("preserve_queue")
        )
        if (
            terminal.get("changed")
            and should_resume
            and (current is None or current.logical_turn_id != logical_turn_id)
        ):
            asyncio.create_task(
                self._resume_after_native_terminal(session_id, logical_turn_id),
                name=f"durable-result-resume:{session_id}",
            )

    async def _finish_durable_terminal_after_release(
        self,
        session_id: str,
        logical_turn_id: str,
        sink: dict[str, Any],
        *,
        is_error: bool,
    ) -> None:
        done = sink.get("done_event")
        if done is not None and not done.is_set():
            await done.wait()
        self._finish_durable_terminal_result(
            session_id,
            logical_turn_id,
            is_error=is_error,
            settled_by=str(sink.get("settled_by") or "") or None,
        )

    def on_terminal_result(self, context: "MessageContext", *, is_error: bool) -> None:
        """OUTBOUND turn chokepoint for the active terminal ``result``."""
        if self.controller is None:
            return
        if not self.is_active_emit(context):
            return
        # The dispatcher calls this before delivery. Record authority now, then
        # checkpoint from Controller.emit_agent_message's post-delivery finally.
        self._set_context_flag(context, _SHOW_CHECKPOINT_TERMINAL_PENDING_KEY, True)
        session_id = self.controller._session_id_from_context(context)
        if not session_id:
            return
        logical_turn_id = str(
            (getattr(context, "platform_specific", None) or {}).get("turn_token")
            or ""
        ).strip()
        if logical_turn_id:
            current = self.in_flight.get(session_id)
            if current is not None and current.logical_turn_id == logical_turn_id:
                current.terminal_is_error = current.terminal_is_error or is_error
            sink = None
            get_sink = getattr(self.controller, "get_turn_sink", None)
            get_key = getattr(self.controller, "_get_session_key", None)
            if callable(get_sink) and callable(get_key):
                try:
                    sink = get_sink(get_key(context))
                except Exception:
                    logger.debug("failed to inspect terminal Turn sink", exc_info=True)
            if isinstance(sink, dict) and sink.get("done_event") is not None:
                asyncio.create_task(
                    self._finish_durable_terminal_after_release(
                        session_id,
                        logical_turn_id,
                        sink,
                        is_error=is_error,
                    ),
                    name=f"durable-result-reconcile:{session_id}",
                )
            else:
                self._finish_durable_terminal_result(
                    session_id,
                    logical_turn_id,
                    is_error=is_error,
                    settled_by=(
                        current.cancel_settled_by if current is not None else None
                    ),
                )
        self.controller.set_agent_status(session_id, "failed" if is_error else "idle")

    def on_terminal_delivery_complete(self, context: "MessageContext") -> None:
        """Checkpoint an accepted terminal result after delivery and persistence."""

        if not self._pop_context_flag(context, _SHOW_CHECKPOINT_TERMINAL_PENDING_KEY):
            return
        self._end_show_checkpoint(context)

    def register_agent_initiated_turn(self, context: "MessageContext") -> bool:
        """Register a turn the BACKEND started on its own (agent-initiated:
        background-task completion / ScheduleWakeup) as a first-class FSM citizen,
        so the Workbench Stop button works and the browser sees ``turn.start`` /
        ``turn.end``.

        Unlike a user / scheduled turn there is NO dispatch task sending a query —
        the backend already started — so this does NOT go through ``dispatch_turn`` /
        ``_run``. The unsolicited output is ALREADY streaming on the long-lived
        receiver, so the sink + ``in_flight`` are registered SYNCHRONOUSLY here
        (before the receiver's next emit), and a small holder task keeps the turn
        open until the terminal result's ``done_event``. Settling (pop sink +
        ``in_flight`` + ``turn.end`` + flush) mirrors ``_run``'s finally. Stop works
        because ``cancel`` interrupts the backend via ``handle_stop(turn.context)``
        and cancels this holder.

        avibe-only: a turn with no workbench session id (IM / CLI) has no Stop
        control and no sink, so this is a no-op there — the gate + outbound
        chokepoint still deliver the reply. Returns ``True`` when a turn was
        registered.
        """
        if self.controller is None:
            return False
        session_id = self.controller._session_id_from_context(context)
        if not session_id:
            return False
        get_key = getattr(self.controller, "_get_session_key", None)
        if not callable(get_key):
            return False
        session_key = get_key(context)
        # Defensive: ``begin_agent_initiated_turn`` only opens on a free gate, so a
        # turn shouldn't already be tracked/streaming — but never clobber one.
        if session_id in self.in_flight or self.get_turn_sink(session_key) is not None:
            return False
        from core.inbox_events import bus

        payload = getattr(context, "platform_specific", None) or {}
        turn_token = str(payload.get("turn_token") or delivery_store.new_turn_id())
        if context.platform_specific is None:
            context.platform_specific = {}
        context.platform_specific["turn_token"] = turn_token
        engine = self._sqlite_engine()
        durable_schema = self._durable_schema_available()
        with engine.connect() as conn:
            session_exists = durable_schema and (
                conn.execute(
                    select(agent_sessions.c.id).where(agent_sessions.c.id == session_id)
                ).scalar_one_or_none()
                is not None
            )
        durable_turn_registered = False
        materialized_delivery_id: str | None = None
        if session_exists:
            with engine.begin() as conn:
                reserve_write_lock(conn)
                durable = delivery_store.get_turn(conn, turn_token)
                if durable is None:
                    if delivery_store.active_turn(conn, session_id) is not None:
                        return False
                    session_row = conn.execute(
                        select(
                            agent_sessions.c.scope_id,
                            agent_sessions.c.status,
                        ).where(agent_sessions.c.id == session_id)
                    ).mappings().one()
                    if session_row["status"] != "active":
                        return False
                    delivery_id = delivery_store.new_delivery_id()
                    language_getter = getattr(self.controller, "_get_lang", None)
                    language = (
                        language_getter()
                        if callable(language_getter)
                        else getattr(
                            getattr(self.controller, "config", None),
                            "language",
                            "en",
                        )
                    )
                    trigger_text = str(
                        payload.get("agent_initiated_trigger_text")
                        or i18n_t("harness.agentInitiatedContinuation", language)
                    )
                    delivery_store.insert_delivery(
                        conn,
                        delivery_id=delivery_id,
                        session_id=session_id,
                        priority="p3",
                        state="start_attempting",
                        snapshot=delivery_store.message_snapshot(
                            scope_id=session_row["scope_id"],
                            session_id=session_id,
                            platform="avibe",
                            author="harness",
                            source="harness",
                            message_type="harness",
                            text=trigger_text,
                            metadata={
                                "source": "agent_initiated",
                                "runtime_key": payload.get("agent_runtime_turn_key"),
                            },
                        ),
                        dispatch_text="",
                        current_attempt_id=delivery_store.new_attempt_id(),
                        current_attempt_kind="start",
                        current_target_turn_id=turn_token,
                        history_event={
                            "kind": "start",
                            "turn_id": turn_token,
                            "outcome": "backend_initiated",
                        },
                    )
                    delivery_store.insert_turn(
                        conn,
                        turn_id=turn_token,
                        session_id=session_id,
                        initial_delivery_id=delivery_id,
                        state="starting",
                        backend=self._context_backend(context),
                    )
                    durable = delivery_store.get_turn(conn, turn_token)
                if durable is None:
                    return False
                bound = delivery_store.bind_native_start(
                    conn,
                    turn_token,
                    expected_version=int(durable["version"]),
                    runtime_key=str(payload.get("agent_runtime_turn_key") or "agent-initiated"),
                    runtime_turn_id=str(payload.get("agent_runtime_turn_token") or "agent-initiated"),
                    native_turn_id=None,
                )
                durable_turn_registered = bound is not None
                if bound is not None:
                    initial = delivery_store.delivery_for_turn(conn, turn_token)
                    materialized = (
                        delivery_store.materialize_acceptance(
                            conn,
                            delivery_id=str(initial["id"]),
                            expected_attempt_id=str(
                                initial.get("current_attempt_id") or ""
                            )
                            or None,
                            accepted_turn_id=turn_token,
                            evidence={"kind": "backend_initiated_output"},
                        )
                        if initial is not None
                        else None
                    )
                    if materialized is None:
                        raise RuntimeError(
                            "agent-initiated Turn could not materialize its trigger Delivery"
                        )
                    materialized_delivery_id = str(materialized["id"])
            if not durable_turn_registered:
                return False
            if materialized_delivery_id:
                context.message_id = materialized_delivery_id
                self._publish_materialized_delivery(materialized_delivery_id)
        done = asyncio.Event()
        self.register_turn_sink(
            session_key,
            on_chunk=self._noop_chunk,
            done_event=done,
            turn_token=turn_token,
            context=context,
        )

        async def _holder() -> None:
            cancelled = False
            try:
                await done.wait()
            except asyncio.CancelledError:
                cancelled = True
                raise
            finally:
                sink = self.get_turn_sink(session_key)
                settled_by = str((sink or {}).get("settled_by") or "")
                self.pop_turn_sink(session_key, done)
                current = self.in_flight.get(session_id)
                turn = current if current is not None and current.task is asyncio.current_task() else None
                terminal_is_error = bool(
                    turn is not None and turn.terminal_is_error
                )
                if turn is not None:
                    self.in_flight.pop(session_id, None)
                if turn is not None:
                    bus.publish("turn.end", {"session_id": session_id})
                if durable_turn_registered:
                    try:
                        self._terminalize_durable_turn(
                            turn_token,
                            self._durable_terminal_outcome(
                                is_error=terminal_is_error,
                                settled_by=(
                                    SETTLED_BY_STOPPED
                                    if cancelled
                                    else settled_by or None
                                ),
                            ),
                            settled_by=(
                                SETTLED_BY_STOPPED
                                if cancelled
                                else settled_by or SETTLED_BY_TERMINAL_RESULT
                            ),
                            evidence_kind="agent_initiated_terminal",
                            hold_queue=bool(
                                (cancelled or settled_by == SETTLED_BY_STOPPED)
                                and turn is not None
                                and turn.stop_no_flush
                                and not turn.flush_on_cancel
                            ),
                        )
                    except Exception:
                        logger.exception(
                            "agent-initiated durable terminal reconciliation deferred "
                            "for Turn=%s",
                            turn_token,
                        )
                # Flush the send-while-busy queue on NATURAL completion (mirrors
                # ``_run``): a plain Stop keeps the queue, send_now opts back in.
                should_flush = (not cancelled and not (turn is not None and turn.stop_no_flush)) or (
                    turn is not None and turn.flush_on_cancel
                )
                if should_flush:
                    try:
                        if durable_turn_registered:
                            await self._resume_post_terminal(session_id)
                        else:
                            await self.flush_queue(session_id)
                    except Exception:
                        logger.debug("agent-initiated turn: queue resume failed", exc_info=True)

        try:
            task = asyncio.create_task(_holder(), name="agent-initiated-turn-holder")
        except RuntimeError:
            # No running loop (sync test/stub context): can't hold the turn open —
            # roll the sink back so it doesn't leak, and skip FSM registration.
            self.pop_turn_sink(session_key, done)
            return False
        self.in_flight[session_id] = Turn(
            task=task,
            context=context,
            started_at=_utc_now_iso(),
            logical_turn_id=turn_token,
        )
        bus.publish("turn.start", {"session_id": session_id})
        if self._pop_context_flag(context, _SHOW_CHECKPOINT_DEFERRED_START_KEY):
            self._begin_show_checkpoint(context)
        return True

    def is_active_emit(self, context: "MessageContext") -> bool:
        """Whether an emit belongs to the live turn (not a superseded one). Fail-open
        when there's no sink registry / no live sink (non-streaming turns still
        settle), else apply the one token rule. Centralizes the old
        ``ConsolidatedMessageDispatcher._is_active_turn``."""
        get_sink = getattr(self.controller, "get_turn_sink", None)
        get_key = getattr(self.controller, "_get_session_key", None)
        if not callable(get_sink) or not callable(get_key):
            return True
        try:
            sink = get_sink(get_key(context))
        except Exception:
            return True
        if sink is None:
            return True
        return emit_matches_active_turn(sink, context)

    # --- the live streaming turn sink (owned here; Controller delegates) ----------

    @staticmethod
    def _turn_sink_identity(context: Optional["MessageContext"]) -> dict[str, Any]:
        raw_spec = getattr(context, "platform_specific", None) or {}
        spec = raw_spec if isinstance(raw_spec, dict) else {}
        target = spec.get("agent_session_target")
        target = target if isinstance(target, dict) else {}
        agent_session_id = str(spec.get("agent_session_id") or target.get("id") or "").strip()
        backend_base_session_id = str(
            spec.get("backend_base_session_id")
            or target.get("session_anchor")
            or ""
        ).strip()
        identity: dict[str, Any] = {
            "agent_session_id": agent_session_id,
            "backend_base_session_id": backend_base_session_id,
        }
        task_trigger_kind = str(spec.get("task_trigger_kind") or "").strip()
        if task_trigger_kind:
            identity["task_trigger_kind"] = task_trigger_kind
        task_execution_id = str(spec.get("task_execution_id") or "").strip()
        if task_execution_id:
            identity["task_execution_id"] = task_execution_id
        coalesced_queue = spec.get("coalesced_queue")
        if isinstance(coalesced_queue, dict):
            copied_queue = dict(coalesced_queue)
            execution_ids = copied_queue.get("execution_ids")
            if isinstance(execution_ids, list):
                copied_queue["execution_ids"] = list(execution_ids)
            identity["coalesced_queue"] = copied_queue
        elif coalesced_queue is not None:
            identity["coalesced_queue"] = coalesced_queue
        return identity

    def register_turn_sink(self, session_key: str, *, on_chunk, done_event, turn_token=None, context=None) -> None:
        if session_key in self.active_turn_sinks:
            # dispatch_turn serializes streaming turns per session, so this should not
            # happen; if it does, keep the in-flight turn's sink rather than clobbering
            # it (replacing it once let a stale result satisfy a replacement sink).
            logger.warning("Ignoring duplicate turn sink registration for %s", session_key)
            return
        # turn_token correlates emits to this exact turn so a late straggler from a
        # superseded turn (same session key) is dropped in _stream_chunk.
        identity = self._turn_sink_identity(context)
        self.active_turn_sinks[session_key] = {
            "on_chunk": on_chunk,
            "done_event": done_event,
            "turn_token": turn_token,
            **identity,
        }

    def pop_turn_sink(self, session_key: str, done_event=None) -> None:
        # Identity-guarded: only remove the sink THIS turn registered. A concurrent /
        # retried turn may have replaced it (same session key, different done_event);
        # the older turn's cleanup must not evict the newer turn's sink. done_event=None
        # pops unconditionally (non-streaming / legacy callers).
        sink = self.active_turn_sinks.get(session_key)
        if sink is None:
            return
        if done_event is not None and sink.get("done_event") is not done_event:
            return
        self.active_turn_sinks.pop(session_key, None)

    def get_turn_sink(self, session_key: str) -> Optional[dict]:
        return self.active_turn_sinks.get(session_key)

    @staticmethod
    def _sink_identity_matches(
        sink: dict,
        *,
        agent_session_id: Optional[str],
        backend_base_session_id: Optional[str],
    ) -> bool:
        expected_session = str(agent_session_id or "").strip()
        expected_base = str(backend_base_session_id or "").strip()
        if expected_session and sink.get("agent_session_id") != expected_session:
            return False
        if expected_base and sink.get("backend_base_session_id") != expected_base:
            return False
        return True

    def bind_context_to_turn_sink(
        self,
        context: "MessageContext",
        *,
        agent_session_id: Optional[str] = None,
        backend_base_session_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Stamp the live sink's token onto an external stop context.

        Running-tab End may stop an agent-run turn from outside the original
        dispatch context. Rebuild the context to the same session key, require the
        registered sink's session/base identity to match the clicked row, then copy
        that sink's token so the backend's silent terminal result satisfies the
        normal active-turn guard. The returned binding can be used as an
        identity-guarded fallback if the backend stops without emitting.
        """
        if self.controller is None:
            return None
        get_key = getattr(self.controller, "_get_session_key", None)
        if not callable(get_key):
            return None
        try:
            session_key = get_key(context)
        except Exception:
            logger.debug("turn sink bind: failed to derive session key", exc_info=True)
            return None
        sink = self.active_turn_sinks.get(session_key)
        if sink is None:
            return None
        if not self._sink_identity_matches(
            sink,
            agent_session_id=agent_session_id,
            backend_base_session_id=backend_base_session_id,
        ):
            return None
        token = sink.get("turn_token")
        attribution_keys = ("task_trigger_kind", "task_execution_id", "coalesced_queue")
        if token or any(key in sink for key in attribution_keys):
            if context.platform_specific is None:
                context.platform_specific = {}
        if token:
            context.platform_specific["turn_token"] = token
        for key in attribution_keys:
            if key not in sink:
                continue
            value = sink[key]
            if isinstance(value, dict):
                copied_value = dict(value)
                execution_ids = copied_value.get("execution_ids")
                if isinstance(execution_ids, list):
                    copied_value["execution_ids"] = list(execution_ids)
                value = copied_value
            context.platform_specific[key] = value
        return {
            "session_key": session_key,
            "sink": sink,
            "done_event": sink.get("done_event"),
            "turn_token": token,
        }

    def settle_bound_turn_sink(self, binding: Optional[dict]) -> bool:
        """Settle the same sink returned by ``bind_context_to_turn_sink``.

        This is a fallback for stop paths that successfully interrupt a backend
        but do not emit a terminal result. It only releases the dispatch waiter;
        run completion is still owned by the backend's terminal emit when one
        arrives, and the bound stop context carries the original agent-run
        attribution so that emit records the run terminal. The identity guard is
        intentionally object-based so a late stop cannot complete a newer sink
        registered under the same session key.

        Ordering against a real terminal result is resolved by precedence, not by
        timing luck. A terminal that lands *before* this call finds ``done`` already
        set and we bail out, leaving ``settled_by="terminal_result"`` so the run
        settles from its true result. A terminal that lands *after* the stop was
        acknowledged cannot take the reason back — the dispatcher refuses to overwrite
        a recorded ``stopped`` — so this run is always settled through the ``canceled``
        mapping (``SETTLEMENT_TERMINAL_STATUS``). Whether ``canceled`` reaches the row
        is then ordinary first-writer-wins: both writers are scoped to
        ``queued|running``, so if the result's own row write got there first the run
        keeps ``succeeded``. That is deliberate rather than lossy — the backend really
        did finish, its text is recorded in the run's outputs either way, and forcing
        ``canceled`` over an existing terminal status would mean breaking the single
        guarantee every other settlement path relies on.
        """
        if not isinstance(binding, dict):
            return False
        session_key = binding.get("session_key")
        sink = binding.get("sink")
        if not session_key or self.active_turn_sinks.get(session_key) is not sink:
            return False
        done = sink.get("done_event") if isinstance(sink, dict) else None
        if done is None or done is not binding.get("done_event"):
            return False
        token = binding.get("turn_token")
        if token is not None and sink.get("turn_token") != token:
            return False
        is_set = getattr(done, "is_set", None)
        if callable(is_set) and is_set():
            return False
        # A stop that interrupted the backend without a terminal result: record it so
        # ``dispatch_turn``'s caller settles the run instead of leaving it ``running``
        # forever. ``setdefault`` keeps an already-recorded real terminal result.
        sink.setdefault("settled_by", SETTLED_BY_STOPPED)
        done.set()
        return True

    # --- boot / restore edge transitions -----------------------------------------

    @staticmethod
    def reset_stale() -> None:
        """Reset only legacy running projections with no durable Turn owner."""
        try:
            engine = get_cached_sqlite_engine()
            with engine.begin() as conn:
                reserve_write_lock(conn)
                live = select(session_turn_rows.c.session_id).where(
                    session_turn_rows.c.state.in_(delivery_store.TURN_OWNER_STATES)
                )
                result = conn.execute(
                    update(agent_sessions)
                    .where(agent_sessions.c.agent_status == "running")
                    .where(~agent_sessions.c.id.in_(live))
                    .values(agent_status="idle")
                )
                reset = result.rowcount or 0
            if reset:
                logger.info(
                    "Reset %s ownerless 'running' agent session(s) to idle on startup",
                    reset,
                )
        except Exception:
            logger.debug("agent_status startup reset failed", exc_info=True)

    def restore_running(self, session_id: Optional[str]) -> None:
        """Re-mark an avibe session ``running`` when its OpenCode poll is restored
        after a restart: the restored poll resumes the backend turn WITHOUT
        re-entering the inbound chokepoint (``handle_message``), so without this the
        dot would read idle for a still-live turn until the poll's terminal result
        settles it back. IM polls carry no workbench session id, so they pass nothing
        here and stay dot-less."""
        if not session_id or self.controller is None:
            return

        async def _reconcile_restored_owner() -> None:
            try:
                await self.recover_durable_delivery_state(session_id)
            except Exception:
                logger.exception(
                    "durable owner reconciliation failed after poll restoration for %s",
                    session_id,
                )
                self.controller.set_agent_status(session_id, "running")
                return
            with self._sqlite_engine().connect() as conn:
                has_history = session_id in delivery_store.session_ids_with_turn_history(conn)
            if not has_history:
                # Pre-0043 polls have backend evidence but no durable logical Turn.
                self.controller.set_agent_status(session_id, "running")

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Synchronous compatibility callers cannot run durable reconciliation.
            self.controller.set_agent_status(session_id, "running")
            return
        loop.create_task(
            _reconcile_restored_owner(),
            name=f"durable-poll-restore:{session_id}",
        )
