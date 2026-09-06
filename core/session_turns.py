"""Per-session turn ownership for the avibe workbench.

Phase 1b of the turn-lifecycle FSM (``docs/plans/avibe-turn-lifecycle-fsm.md``):
introduce ONE owner of a session's turn state so the gate, dispatcher, scheduler,
and restore paths stop reconciling several separate stores. A session has **at
most one active turn** (IDLE ↔ RUNNING; no turn-duration timeout — a long agent
runs until it emits its terminal result or the user Stops it).

``SessionTurnManager`` is wired as ``controller.session_turns``. It is the sole
orchestrator of durable Delivery admission, Turn execution/control, terminal
settlement, and Session backlog policy. Process-local registries project those
owners for live tasks and streaming only.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import uuid
import weakref
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, ContextManager, Iterator, Literal, Optional

from sqlalchemy import and_, exists, literal, or_, select, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from core.web_push_notifications import WEB_PUSH_USER_KEY_METADATA, WEB_PUSH_USER_KEYS_METADATA
from core.delivery_target import normalize_message_kind
from core.agent_input import AgentInputMetadata
from core.message_context import (
    resolve_turn_sink_key,
)
from core.native_dispatch_phase import (
    backend_dispatch_attempted,
    mark_prewrite_user_stop,
)
from core.run_settlement import (
    NON_COMPLETING_TURN_SETTLEMENTS,
    SETTLEMENTS_WITHOUT_RESULT,
    SETTLED_BY_BACKEND_REFRESH,
    SETTLED_BY_NO_TERMINAL_RESULT,
    SETTLED_BY_REFUSED_CONCURRENT_TURN,
    SETTLED_BY_RESTARTED,
    SETTLED_BY_STOPPED,
    SETTLED_BY_TERMINAL_RESULT,
)
from core.processing_indicator import INTERRUPTED_REACTION_EMOJI
from core.services.dispatch import SOURCE_HUMAN, SOURCE_SCHEDULED, dispatch_turn_with_outcome
from core.services.agent_steering import (
    SteerOutcome,
    SteerReconcileRequest,
    SteerRequest,
    SteerResult,
    active_steer_identity,
    reconcile_steer_attempt,
    result as steer_result,
    steer_active_turn,
)
from storage import messages_service
from storage import message_deliveries as delivery_store
from storage.agent_session_rows import reserve_write_lock
from storage.db import get_cached_sqlite_engine
from storage.background import (
    OWED_FAILURE_NOTICE_KEY,
    apply_live_agent_run_cancellation_in_connection,
    normalize_run_status,
    run_update_event_transaction,
)
from storage.session_reclaim import reconcile_explicit_overrides
from storage.models import (
    agent_runs,
    agent_sessions,
    message_deliveries as delivery_rows,
    session_turns as session_turn_rows,
)
from storage.workbench_sessions_service import derive_session_harness_activities
from core.message_output import terminal_turn_output
from core.runtime_activation import (
    RuntimeActivationIdentity,
    RuntimeActivationRegistry,
    RuntimeActivationResolution,
)
from vibe.i18n import t as i18n_t

if TYPE_CHECKING:
    from modules.im import MessageContext

logger = logging.getLogger(__name__)


@dataclass
class _SessionLifecycleState:
    """One live session generation retained only by active lifecycle work."""

    admission_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    admission_waiters: int = 0
    epoch: int = 0


@dataclass(frozen=True, slots=True)
class SessionLifecycleSnapshot:
    """Strong reference proving which live session generation admitted a turn."""

    _state: _SessionLifecycleState
    epoch: int


@dataclass
class TurnLifecycleAdmission:
    """One idempotent lease bridging turn admission into Memory capture."""

    _state: _SessionLifecycleState
    _released: bool = field(default=False, init=False)

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._state.admission_lock.release()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _accepted_steer_receipt(delivery: dict[str, Any]) -> SteerResult:
    """Rebuild a typed accepted receipt from durable evidence."""

    try:
        payload = json.loads(str(delivery.get("current_receipt_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    details = payload.get("details")
    if not isinstance(details, dict):
        details = {}
    return steer_result(
        SteerOutcome.ACCEPTED,
        reason=(
            str(payload.get("reason"))
            if payload.get("reason") is not None
            else None
        ),
        **details,
    )


def _turn_event_payload(session_id: str, turn_id: str | None = None) -> dict[str, str]:
    payload = {"session_id": session_id}
    if turn_id:
        payload["turn_id"] = turn_id
    return payload


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
_TERMINAL_RESULT_LATCH_KEY = "_avibe_terminal_result_latch"

# The platform_specific keys the FLUSH rebuilds fresh from the session row (avibe
# routing). Everything ELSE the scheduled context carries is delivery / attribution
# provenance to preserve. We capture by EXCLUDING these (a blocklist) rather than
# whitelisting provenance keys, so a delivery field like ``delivery_override`` — what
# ``MessageDispatcher._get_target_context`` actually redirects delivery on — can't be
# silently omitted (Codex P1 #3338692433).
_FLUSH_REBUILT_KEYS = frozenset(
    {"platform", "is_dm", "workbench_session_id", "agent_session_id", "agent_session_target", "turn_token"}
)
_EXECUTION_ROUTING_KEYS = _FLUSH_REBUILT_KEYS | frozenset(
    {
        "vibe_agent_id",
        "vibe_agent_name",
        "scheduled_target_agent_name",
        "resolved_vibe_agent",
    }
)
SCHEDULED_TARGET_AGENT_KEY = "scheduled_target_agent_name"

_NON_RESTORABLE_RUNTIME_BACKENDS = frozenset({"claude", "codex"})
_MAX_AUTOMATIC_UNKNOWN_START_REPLAYS = 1
_UNKNOWN_START_REPLAY_INSTRUCTION = (
    "[Avibe recovery: this request may have been delivered before restart. "
    "Before any irreversible action, check whether the work is already complete.]\n\n"
)


@dataclass(frozen=True)
class RuntimeDeliveryObservation:
    session_id: str
    kind: str
    delivery_id: str | None
    delivery_state: str | None
    delivery_version: int | None
    delivery_attempt_id: str | None
    delivery_target_turn_id: str | None
    delivery_expected_native_turn_id: str | None
    turn_id: str | None
    turn_state: str | None
    turn_version: int | None
    start_attempt_id: str | None
    native_turn_id: str | None
    predecessor_turn_id: str | None = None
    predecessor_state: str | None = None
    predecessor_version: int | None = None
    predecessor_control_state: str | None = None
    predecessor_control_mode: str | None = None
    predecessor_control_attempt_id: str | None = None
    predecessor_control_expected_native_turn_id: str | None = None
    predecessor_control_receipt_outcome: str | None = None
    predecessor_successor_delivery_id: str | None = None
    predecessor_successor_turn_id: str | None = None
    predecessor_terminal_outcome: str | None = None
    predecessor_settled_by: str | None = None
    predecessor_terminal_evidence_kind: str | None = None
    predecessor_terminal_evidence_json: str | None = None
    predecessor_terminal_at: str | None = None


def _start_replay_count(deliveries: list[dict[str, Any]]) -> int:
    counts: list[int] = []
    for delivery in deliveries:
        try:
            history = json.loads(str(delivery.get("delivery_history_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            history = {}
        events = history.get("events") if isinstance(history, dict) else None
        counts.append(
            sum(
                1
                for event in events or []
                if isinstance(event, dict)
                and event.get("kind") == "start"
                and event.get("outcome") == "restart_replayed"
            )
        )
    return max(counts, default=0)


def _opencode_native_session_id(native_turn_id: str | None) -> str | None:
    value = str(native_turn_id or "").strip()
    if not value.startswith("opencode:"):
        return None
    native_session_id, separator, generation = value[len("opencode:") :].rpartition(":")
    if not separator or not native_session_id or not generation:
        return None
    return native_session_id


def _same_opencode_native_session(
    persisted_native_turn_id: str | None,
    restored_native_turn_id: str | None,
) -> bool:
    persisted_session = _opencode_native_session_id(persisted_native_turn_id)
    return bool(
        persisted_session
        and persisted_session == _opencode_native_session_id(restored_native_turn_id)
    )


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


def _parse_queue_timestamp(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _scheduled_provenance(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    metadata = row.get("metadata") or {}
    provenance = metadata.get(SCHEDULED_PROVENANCE_KEY)
    return provenance if isinstance(provenance, dict) else None


def _agent_run_merge_definition_id(spec: dict[str, Any]) -> str:
    return "agent_run"


def _scheduled_merge_key(row: dict[str, Any]) -> Optional[tuple[str, ...]]:
    provenance = _scheduled_provenance(row)
    if provenance is None:
        return None
    spec = provenance.get("platform_specific") or {}
    if not isinstance(spec, dict):
        return None
    trigger_kind = str(spec.get("task_trigger_kind") or "").strip()
    definition_id = str(spec.get("task_definition_id") or "").strip()
    if trigger_kind == "agent_run" and not definition_id:
        definition_id = _agent_run_merge_definition_id(spec)
    if not trigger_kind or not definition_id:
        return None
    delivery_override = (
        spec.get("delivery_override")
        if isinstance(spec.get("delivery_override"), dict)
        else {}
    )
    delivery_alias = (
        spec.get("scheduled_delivery_alias")
        if isinstance(spec.get("scheduled_delivery_alias"), dict)
        else {}
    )
    stable_agent_key = str(spec.get("vibe_agent_id") or "").strip()
    if not stable_agent_key:
        stable_agent_key = str(
            spec.get("vibe_agent_name")
            or spec.get(SCHEDULED_TARGET_AGENT_KEY)
            or ""
        )
    source_session_id = str(spec.get("source_session_id") or "").strip()
    if not source_session_id and str(spec.get("source_kind") or "").strip() == "agent":
        source_session_id = str(spec.get("source_actor") or "").strip()
    return (
        trigger_kind,
        definition_id,
        stable_agent_key,
        source_session_id,
        str(spec.get("delivery_key_external") or ""),
        str(spec.get("delivery_scope_session_key") or ""),
        str(delivery_override.get("platform") or ""),
        str(delivery_override.get("user_id") or ""),
        str(delivery_override.get("channel_id") or ""),
        str(delivery_override.get("thread_id") or ""),
        str(delivery_alias.get("mode") or ""),
        str(delivery_alias.get("clear_source") or ""),
        str(bool(spec.get("suppress_delivery"))),
    )


def _collect_delivery_segment(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the one compatible leading segment a Turn may claim."""

    if not rows:
        return []
    message_identity = delivery_store.message_merge_identity(rows[0])
    scheduled_key = _scheduled_merge_key(rows[0])
    if _scheduled_provenance(rows[0]) is not None and scheduled_key is None:
        return [rows[0]]
    if scheduled_key is not None:
        segment = [rows[0]]
        latest = rows[0]
        for row in rows[1:]:
            previous = _parse_queue_timestamp(latest.get("submitted_at"))
            current = _parse_queue_timestamp(row.get("submitted_at"))
            if (
                delivery_store.message_merge_identity(row) != message_identity
                or _scheduled_merge_key(row) != scheduled_key
                or previous is None
                or current is None
            ):
                break
            delta = (current - previous).total_seconds()
            if delta < 0 or delta > SCHEDULED_QUEUE_MERGE_WINDOW_SECONDS:
                break
            segment.append(row)
            latest = row
        return segment

    segment: list[dict[str, Any]] = []
    for row in rows:
        if _scheduled_provenance(row) is not None:
            break
        if delivery_store.message_merge_identity(row) != message_identity:
            break
        native_message_id = str(row.get("native_message_id") or "").strip()
        if native_message_id:
            if not segment:
                segment.append(row)
            break
        segment.append(row)
    return segment


def _delivery_dispatch_text(delivery: dict[str, Any]) -> str:
    text = str(delivery.get("dispatch_text") or "")
    admission = delivery_store.delivery_admission_context(delivery)
    route = admission.get("message_handler_route")
    if not isinstance(route, dict):
        return text
    payload = delivery_store.delivery_payload(delivery)
    if payload.get("source") != "user" or payload.get("platform") == "avibe":
        return text
    from core.agent_input import without_legacy_metadata
    from modules.agents.subagent_router import parse_subagent_prefix

    original = str(payload.get("text") or "")
    if route.get("subagent_key"):
        parsed = parse_subagent_prefix(original)
        if parsed is not None:
            original = parsed.message
    return without_legacy_metadata(text, original=original, user_id=str(payload.get("author_id") or ""))


def _segment_dispatch_text(segment: list[dict[str, Any]]) -> str:
    texts = [_delivery_dispatch_text(row) for row in segment]
    texts = [text for text in texts if text.strip()]
    leader = segment[0] if segment else {}
    if "snapshot_json" in leader:
        leader = delivery_store.delivery_payload(leader)
    scheduled = _scheduled_merge_key(leader) is not None
    return ("\n\n---\n\n" if scheduled else "\n").join(texts)


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
    Persistent queue policy is deliberately absent. Durable Delivery/Turn ownership
    decides whether backlog may drain across both process lifetime and restart.

    The streaming SINK is deliberately NOT held here: it is keyed by the
    thread-scoped turn-sink key (platform-prefixed, e.g. ``avibe::<id>``) not
    ``session_id``, is registered from the dispatcher on the emit path, and is
    platform-agnostic (an IM stream has a sink but no avibe ``session_id``). See
    ``SessionTurnManager.active_turn_sinks``.
    """

    task: asyncio.Task
    context: "MessageContext"
    started_at: str = ""
    #: WHY this turn's task was cancelled, in the ``core.run_settlement``
    #: vocabulary — set by the canceller BEFORE ``task.cancel()`` so ``_run`` can
    #: attribute the run it owns correctly. User interruption paths set
    #: ``SETTLED_BY_STOPPED`` before invoking the backend because a successful Stop
    #: may emit its terminal result before ``handle_stop`` returns; backend runtime
    #: refresh sets ``SETTLED_BY_BACKEND_REFRESH`` so a routine ``agents.*``
    #: reconciliation is not reported as if the user pressed Stop (Codex P1). It
    #: rides on the Turn so it retires when the turn is popped.
    cancel_settled_by: Optional[str] = None
    #: Transient instruction from the cancellation owner. Service teardown and
    #: backend drain must not start replacement work in the process they are
    #: stopping, even when the semantic outcome is a user Stop. This is Turn-local
    #: cancellation state, not a persistent Session queue hold.
    cancel_defers_queue_resume: bool = False
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
    expected_delivery_id: str | None = None
    expected_turn_id: str | None = None
    # Run-level cancellation may interrupt a backend only when this exact Run is
    # still the Turn's sole initial input.  Checked under the P0 writer lock.
    expected_exclusive_agent_run_id: str | None = None
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
    admission_context: dict[str, Any] | None = None
    native_message_id: str | None = None
    parent_native_message_id: str | None = None
    message_kind: str = "unknown"
    # A caller that will immediately promote the durable FIFO head can request
    # P3 admission without the usual idle-session auto-start. This keeps the
    # admission and promotion decision on one post-admission queue snapshot.
    admission_only: bool = False


@dataclass(frozen=True)
class DeliveryResult:
    delivery_id: str | None
    message_id: str | None
    state: str
    turn_id: str | None = None
    reason: str | None = None
    # How this input reached its Turn. ``accepted`` alone cannot tell a Delivery
    # that STARTED its own Turn from one that was STEERED into a Turn already
    # running: both settle as ``accepted``. Surfaces that report the admission
    # back to the user need that distinction, because a started Turn already
    # owns its own processing indicator while a steered input has none.
    admission: Literal["", "started", "steered"] = ""


@dataclass(frozen=True)
class TurnSubmissionResult:
    """Routing decision plus the durable queue / delivery-intent outcome."""

    route: Literal["ran", "enqueued"]
    queue_persisted: bool | None = None
    target_was_busy: bool = False
    delivery_status: str | None = None
    delivery_owner_transferred: bool = False


@dataclass(frozen=True)
class _RuntimeStartOwner:
    """Proof that one exact Session runtime boundary owns this transaction."""

    session_id: str
    backend: str
    session_anchor: str
    workdir: str | None
    admitted: bool
    identity: RuntimeActivationIdentity | None = None


class SessionTurnManager:
    """Owns the live per-session turn state + lifecycle for avibe sessions.

    State (a session has at most one active turn):

    - ``in_flight``: ``session_id -> Turn`` for the active turn — the Stop target
      (``/internal/cancel``) and the ``/turn-state`` projection.
    - ``active_turn_sinks``: the live streaming sink per turn-sink key — the
      streaming half, kept separate on purpose (see ``Turn``).

    ``controller`` reaches the backends + the outbound chokepoint
    (``emit_agent_message``); ``build_context`` rebuilds a session's routing
    ``MessageContext`` for a queued follow-up (injected by the gate because it
    lives in ``internal_server``).
    """

    # Backoff for re-sending an interruption notice the transport claimed to be
    # ready for and then failed to deliver. Short enough to catch a blip, long
    # enough that a hard outage does not spin; a class attribute so tests can
    # shrink it instead of sleeping.
    LOST_TURN_RETRY_DELAYS: tuple[float, ...] = (5.0, 30.0, 120.0)

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
        self._session_lifecycle_states: weakref.WeakValueDictionary[
            str, _SessionLifecycleState
        ] = weakref.WeakValueDictionary()
        # Interruption reports owed to turns whose platform was not connected yet
        # when recovery ran, keyed by platform. See ``_report_lost_im_turn``.
        self._pending_lost_turn_reports: dict[str, list[tuple[str, str]]] = {}
        # One in-flight retry task per platform for the reports above.
        self._lost_turn_retry_tasks: dict[str, asyncio.Task[None]] = {}
        # The live turn sink per TURN SINK KEY. Each is
        # ``{on_chunk, done_event, turn_token}`` — the turn's stream callback +
        # completion event + correlation token. Every dispatched turn registers one,
        # IM/CLI included (``_run`` always passes ``_noop_chunk`` so the turn stays
        # open until the backend's terminal result), which makes this map the
        # turn-concurrency slot for ALL surfaces, not just web Chat.
        #
        # Keyed by ``controller._get_turn_sink_key`` — the THREAD-scoped key, not the
        # channel-scoped ``_get_session_key``. Stable across a session's turns, so a
        # reused agent receiver carrying a stale per-turn context still resolves the
        # current turn's sink; distinct per thread, so a busy forum topic no longer
        # occupies its siblings' slots.
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

    def _session_lifecycle_state(
        self,
        raw_session_id: str,
    ) -> _SessionLifecycleState:
        if not isinstance(raw_session_id, str) or not raw_session_id:
            raise ValueError("session lifecycle requires a session id")
        state = self._session_lifecycle_states.get(raw_session_id)
        if state is None:
            state = _SessionLifecycleState()
            self._session_lifecycle_states[raw_session_id] = state
        return state

    def snapshot_session_lifecycle(
        self,
        raw_session_id: str,
    ) -> SessionLifecycleSnapshot:
        """Retain the generation that may attribute one optional capture."""

        state = self._session_lifecycle_state(raw_session_id)
        return SessionLifecycleSnapshot(state, state.epoch)

    def session_lifecycle_snapshot_matches(
        self,
        raw_session_id: str,
        snapshot: object,
    ) -> bool:
        """Revalidate a retained generation before Memory attribution."""

        if not isinstance(raw_session_id, str) or not raw_session_id:
            raise ValueError("session lifecycle requires a session id")
        if not isinstance(snapshot, SessionLifecycleSnapshot):
            return False
        state = self._session_lifecycle_states.get(raw_session_id)
        return (
            state is snapshot._state
            and snapshot._state.epoch == snapshot.epoch
        )

    async def acquire_lifecycle_admission(
        self,
        raw_session_id: str,
    ) -> TurnLifecycleAdmission:
        """Best-effort lease so a capture can quiesce before a destructive op.

        Turn dispatch must not await this lock. Capture tasks acquire it on
        their own task so a hung sidecar cannot fence the next message.
        """

        state = self._session_lifecycle_state(raw_session_id)
        state.admission_waiters += 1
        try:
            await state.admission_lock.acquire()
        except BaseException:
            state.admission_waiters -= 1
            raise
        state.admission_waiters -= 1
        return TurnLifecycleAdmission(state)

    def _advance_session_lifecycle(
        self,
        raw_session_id: str,
        state: _SessionLifecycleState,
        *,
        abandon_captures: bool = False,
    ) -> None:
        """Invalidate retained snapshots; cancel captures only when asked."""

        if self._session_lifecycle_states.get(raw_session_id) is not state:
            raise RuntimeError("session lifecycle ownership changed")
        state.epoch += 1
        if abandon_captures:
            adapter = getattr(self.controller, "memory_adapter", None)
            abandon = getattr(adapter, "abandon_memory_captures_for_session", None)
            if callable(abandon):
                abandon(raw_session_id)

    async def run_session_lifecycle(
        self,
        raw_session_id: str,
        operation: Callable[[], Awaitable[Any]],
        *,
        deadline_seconds: float = 5.0,
    ) -> Any:
        """Run a destructive transition without waiting for Memory capture."""

        state = self._session_lifecycle_state(raw_session_id)
        await state.operation_lock.acquire()
        admission = None
        try:
            pre_epoch = state.epoch
            # Lifecycle operations are intentionally non-blocking with respect
            # to Memory delivery. If a capture already owns the admission lock,
            # advance the generation immediately; the capture will revalidate
            # its snapshot and drop without provider I/O. An uncontended lock
            # acquisition completes synchronously on this event loop.
            if state.admission_lock.locked() or state.admission_waiters:
                self._advance_session_lifecycle(
                    raw_session_id,
                    state,
                    abandon_captures=True,
                )
            else:
                admission = await self.acquire_lifecycle_admission(raw_session_id)
            result = await operation()
            if state.epoch == pre_epoch:
                self._advance_session_lifecycle(
                    raw_session_id,
                    state,
                )
            return result
        finally:
            if admission is not None:
                admission.release()
            state.operation_lock.release()

    @staticmethod
    def _agent_run_ids_from_spec(spec: Any) -> set[str]:
        """Every ``agent_runs`` id a turn started under this spec is settling."""
        if not isinstance(spec, dict):
            return set()
        found: set[str] = set()
        accepted_ids = spec.get("accepted_agent_run_ids")
        if isinstance(accepted_ids, list):
            for value in accepted_ids:
                execution_id = str(value or "").strip()
                if execution_id:
                    found.add(execution_id)
        primary = str(spec.get("task_execution_id") or "").strip()
        if primary:
            found.add(primary)
        return found

    def _settle_agent_run_ids(
        self,
        run_ids: set[str] | list[str],
        settled_by: Optional[str],
    ) -> None:
        if settled_by not in SETTLEMENTS_WITHOUT_RESULT or not run_ids:
            return
        normalized = sorted({str(run_id) for run_id in run_ids if str(run_id).strip()})
        if not normalized:
            return
        service = (
            getattr(self.controller, "scheduled_task_service", None)
            if self.controller
            else None
        )
        settle = getattr(service, "settle_agent_runs_without_result", None)
        if not callable(settle):
            logger.debug("turn settlement: no harness settlement writer available")
            return
        try:
            settle(normalized, settled_by=settled_by)
        except Exception:
            logger.warning(
                "turn settlement: failed to settle runs %s as %s",
                normalized,
                settled_by,
                exc_info=True,
            )

    def _settle_agent_run_ids_from_terminal_turn(
        self,
        run_ids: set[str] | list[str],
        turn: dict[str, Any],
    ) -> None:
        turn_id = str(turn.get("id") or "").strip()
        durable_ids = self.accepted_agent_run_ids_for_turn(turn_id) if turn_id else []
        normalized = sorted(
            {
                str(run_id)
                for run_id in [*run_ids, *durable_ids]
                if str(run_id).strip()
            }
        )
        if not normalized:
            return
        service = (
            getattr(self.controller, "scheduled_task_service", None)
            if self.controller
            else None
        )
        settle = getattr(service, "settle_agent_runs_from_terminal_turn", None)
        if not callable(settle):
            self._settle_agent_run_ids(normalized, turn.get("settled_by"))
            return
        try:
            evidence = json.loads(str(turn.get("terminal_evidence_json") or "{}"))
            if not isinstance(evidence, dict):
                evidence = {}
        except (TypeError, ValueError, json.JSONDecodeError):
            evidence = {}
        try:
            settle(
                normalized,
                turn_id=str(turn["id"]),
                outcome=str(turn.get("terminal_outcome") or "completed"),
                settled_by=str(turn.get("settled_by") or "") or None,
                evidence_kind=str(turn.get("terminal_evidence_kind") or "") or None,
                evidence=evidence,
            )
        except Exception:
            logger.warning(
                "turn settlement: failed to settle late accepted runs %s from Turn=%s",
                normalized,
                turn.get("id"),
                exc_info=True,
            )

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

        run_ids = self._agent_run_ids_from_spec(getattr(context, "platform_specific", None))
        self._settle_agent_run_ids(run_ids, settled_by)

    def _reconcile_terminal_agent_runs(self) -> None:
        """Retry exact terminal-snapshot settlement before the stale-run sweep."""

        with self._sqlite_engine().connect() as conn:
            pairs = list(
                conn.execute(
                    select(
                        agent_runs.c.id.label("run_id"),
                        delivery_rows.c.turn_id,
                    )
                    .join(
                        delivery_rows,
                        delivery_rows.c.id == agent_runs.c.delivery_id,
                    )
                    .join(
                        session_turn_rows,
                        session_turn_rows.c.id == delivery_rows.c.turn_id,
                    )
                    .where(
                        agent_runs.c.status.in_(
                            ["queued", "pending", "running", "processing"]
                        )
                    )
                    .where(delivery_rows.c.state == "accepted")
                    .where(session_turn_rows.c.state == "terminal")
                ).mappings()
            )
            run_ids_by_turn: dict[str, list[str]] = {}
            terminal_turns: dict[str, dict[str, Any]] = {}
            for pair in pairs:
                turn_id = str(pair["turn_id"] or "")
                run_id = str(pair["run_id"] or "")
                if not turn_id or not run_id:
                    continue
                run_ids_by_turn.setdefault(turn_id, []).append(run_id)
            for turn_id in run_ids_by_turn:
                turn = delivery_store.get_turn(conn, turn_id)
                if turn is not None:
                    terminal_turns[turn_id] = turn
        for turn_id, run_ids in run_ids_by_turn.items():
            turn = terminal_turns.get(turn_id)
            if turn is not None:
                self._settle_agent_run_ids_from_terminal_turn(run_ids, turn)

    @staticmethod
    def _append_accepted_agent_run_ids(spec: dict[str, Any], run_ids: list[str]) -> None:
        accepted = spec.get("accepted_agent_run_ids")
        values = list(accepted) if isinstance(accepted, list) else []
        for run_id in run_ids:
            normalized = str(run_id or "").strip()
            if normalized and normalized not in values:
                values.append(normalized)
        if values:
            spec["accepted_agent_run_ids"] = values

    def _attach_accepted_agent_runs(
        self,
        *,
        session_id: str,
        turn_id: str,
        run_ids: list[str],
        context: Optional["MessageContext"],
    ) -> None:
        if not run_ids:
            return
        projected = self.in_flight.get(session_id)
        if projected is not None and projected.logical_turn_id == turn_id:
            if projected.context.platform_specific is None:
                projected.context.platform_specific = {}
            self._append_accepted_agent_run_ids(
                projected.context.platform_specific,
                run_ids,
            )
        context_token = str(
            ((getattr(context, "platform_specific", None) or {}).get("turn_token") or "")
        )
        if context is not None and context_token == turn_id:
            if context.platform_specific is None:
                context.platform_specific = {}
            self._append_accepted_agent_run_ids(context.platform_specific, run_ids)
        for sink in self.active_turn_sinks.values():
            if str(sink.get("turn_token") or "") == turn_id:
                self._append_accepted_agent_run_ids(sink, run_ids)

    def _project_owned_agent_run_ids(
        self,
        candidate_run_ids: Optional[set[str]] = None,
    ) -> set[str]:
        """Read Run ownership without reconciling or settling lifecycle state."""

        candidates = (
            {str(run_id) for run_id in candidate_run_ids if str(run_id or "").strip()}
            if candidate_run_ids is not None
            else None
        )

        def _eligible(run_ids: set[str]) -> set[str]:
            return run_ids if candidates is None else run_ids & candidates

        owned: set[str] = set()
        for turn in list(self.in_flight.values()):
            owned |= _eligible(
                self._agent_run_ids_from_spec(
                    getattr(turn.context, "platform_specific", None)
                )
            )
        for sink in list(self.active_turn_sinks.values()):
            owned |= _eligible(self._agent_run_ids_from_spec(sink))
        if self._durable_schema_available() and candidates != set():
            stmt = (
                select(agent_runs.c.id)
                .join(
                    delivery_rows,
                    delivery_rows.c.id == agent_runs.c.delivery_id,
                )
                .where(
                    agent_runs.c.status.in_(
                        ["queued", "pending", "running", "processing"]
                    )
                )
                .where(delivery_rows.c.state != "retired")
            )
            if candidates is not None:
                stmt = (
                    stmt.outerjoin(
                        session_turn_rows,
                        session_turn_rows.c.id == delivery_rows.c.turn_id,
                    )
                    .where(agent_runs.c.id.in_(candidates))
                    .where(
                        or_(
                            delivery_rows.c.turn_id.is_(None),
                            session_turn_rows.c.state.in_(
                                delivery_store.TURN_OWNER_STATES
                            ),
                        )
                    )
                )
            with self._sqlite_engine().connect() as conn:
                owned.update(
                    str(run_id)
                    for run_id in conn.execute(stmt).scalars()
                )
        return owned

    def snapshot_owned_agent_run_ids(self, candidate_run_ids: set[str]) -> set[str]:
        """Expose the side-effect-free ownership projection to operator views."""

        return self._project_owned_agent_run_ids(candidate_run_ids)

    def owned_agent_run_ids(self) -> set[str]:
        """Reconcile terminal turns, then return every currently owned Run id."""

        if self._durable_schema_available():
            self._reconcile_terminal_agent_runs()
        return self._project_owned_agent_run_ids()

    def accepted_agent_run_ids_for_turn(self, turn_id: str) -> list[str]:
        """Read restart-stable Run attribution for one exact logical Turn."""

        if not turn_id or not self._durable_schema_available():
            return []
        with self._sqlite_engine().connect() as conn:
            return delivery_store.accepted_agent_run_ids_for_turn(conn, turn_id)

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
    ) -> asyncio.Task[None] | None:
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
            return settle(
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

    async def _steer_input_metadata(self, deliveries: list[dict[str, Any]]) -> AgentInputMetadata | None:
        """Attribute the inserted input to its sender, independently of the active Turn."""
        handler = getattr(self.controller, "message_handler", None)
        prepare = getattr(handler, "prepare_input_metadata", None)
        if not callable(prepare) or not inspect.iscoroutinefunction(prepare):
            return None
        context = self._delivery_context(str(deliveries[0]["session_id"]))
        payload = self._hydrate_delivery_batch_context(context, deliveries)
        scheduled = payload.get("source") == "harness"
        if scheduled:
            self._restore_scheduled_dispatch_context(context, deliveries[0])
        return await prepare(context, human=not scheduled)

    @staticmethod
    def _restore_scheduled_dispatch_context(
        context: "MessageContext",
        delivery: dict[str, Any],
    ) -> None:
        payload = delivery_store.delivery_payload(delivery)
        provenance = (payload.get("metadata") or {}).get(SCHEDULED_PROVENANCE_KEY)
        preserved = (
            provenance.get("platform_specific")
            if isinstance(provenance, dict)
            else None
        )
        if not isinstance(preserved, dict):
            return
        spec = dict(getattr(context, "platform_specific", None) or {})
        spec.update(
            {
                key: value
                for key, value in preserved.items()
                if key not in _EXECUTION_ROUTING_KEYS
            }
        )
        context.platform_specific = spec

    @staticmethod
    def _apply_delivery_binding_provenance(
        context: "MessageContext",
        delivery: dict[str, Any],
    ) -> "MessageContext":
        """Overlay only the exact queued Delivery's routing provenance."""
        provenance = (delivery_store.delivery_payload(delivery).get("metadata") or {}).get(SCHEDULED_PROVENANCE_KEY)
        preserved = provenance.get("platform_specific") if isinstance(provenance, dict) else None
        if not isinstance(preserved, dict):
            return context
        spec = dict(getattr(context, "platform_specific", None) or {})
        for key in (
            "vibe_agent_id",
            "vibe_agent_name",
            SCHEDULED_TARGET_AGENT_KEY,
            "resolved_vibe_agent",
            "agent_run_target",
        ):
            if key in preserved:
                spec[key] = preserved[key]
        context.platform_specific = spec
        return context

    @staticmethod
    def _binding_metadata(binding: dict[str, Any]) -> dict[str, Any]:
        raw_metadata = binding.get("metadata_json")
        try:
            metadata = json.loads(raw_metadata) if raw_metadata else {}
        except (TypeError, ValueError):
            metadata = {}
        return metadata if isinstance(metadata, dict) else {}

    def _context_vibe_agent_binding(self, context: "MessageContext") -> dict[str, str]:
        spec = getattr(context, "platform_specific", None) or {}
        resolver = getattr(self.controller, "resolve_vibe_agent_for_context", None)
        agent = None
        has_explicit_override = False
        if callable(resolver):
            override_agent_id = str(spec.get("vibe_agent_id") or "").strip()
            override_agent_name = str(
                spec.get("vibe_agent_name")
                or spec.get(SCHEDULED_TARGET_AGENT_KEY)
                or ""
            ).strip()
            has_explicit_override = bool(override_agent_id or override_agent_name)
            resolve_kwargs: dict[str, Any] = {"required": has_explicit_override}
            if override_agent_id:
                resolve_kwargs["override_agent_id"] = override_agent_id
            if override_agent_name:
                resolve_kwargs["override_agent_name"] = override_agent_name
            try:
                agent = resolver(context, **resolve_kwargs)
            except Exception:
                if has_explicit_override:
                    raise
                logger.debug(
                    "Failed to resolve inherited Vibe Agent before Session binding",
                    exc_info=True,
                )
            if has_explicit_override and agent is None:
                raise RuntimeError("Explicit Vibe Agent override could not be resolved")
        if agent is not None:
            return {
                "agent_id": str(getattr(agent, "id", None) or "").strip(),
                "agent_name": str(getattr(agent, "name", None) or "").strip(),
                "agent_backend": str(getattr(agent, "backend", None) or "").strip(),
            }
        resolved = spec.get("resolved_vibe_agent")
        if isinstance(resolved, dict):
            return {
                "agent_id": str(resolved.get("id") or "").strip(),
                "agent_name": str(resolved.get("name") or "").strip(),
                "agent_backend": str(resolved.get("backend") or "").strip(),
            }
        return {"agent_id": "", "agent_name": "", "agent_backend": ""}

    @staticmethod
    def _binding_projection_is_stale(
        context: "MessageContext",
        binding: dict[str, Any],
    ) -> bool:
        spec = getattr(context, "platform_specific", None) or {}

        # Scheduled/CLI deliveries may intentionally run a Session through a
        # different Agent for this one execution. Their top-level identity is the
        # explicit request, not a stale copy of the Session row, so durable context
        # refresh must not overwrite it.
        if (spec.get("task_trigger_kind") or spec.get("turn_source") == SOURCE_SCHEDULED) and any(
            spec.get(key) for key in ("vibe_agent_id", "vibe_agent_name")
        ):
            for key, binding_key in (
                ("vibe_agent_id", "agent_id"),
                ("vibe_agent_name", "agent_name"),
            ):
                supplied = str(spec.get(key) or "").strip()
                if supplied and supplied != str(binding.get(binding_key) or "").strip():
                    return False
            if (
                spec.get(SCHEDULED_TARGET_AGENT_KEY)
                and str(spec.get(SCHEDULED_TARGET_AGENT_KEY)).strip() != str(binding.get("agent_name") or "").strip()
            ):
                return False

        durable_session_id = str(binding.get("id") or "").strip()
        for projection, identity_key in (
            (spec.get("agent_session_target"), "id"),
            (spec.get("agent_run_target"), "agent_session_id"),
        ):
            if not isinstance(projection, dict):
                continue
            projection_id = str(projection.get(identity_key) or "").strip()
            if projection_id and durable_session_id and projection_id != durable_session_id:
                return False

        def differs(
            projection: dict[str, Any],
            *,
            identity_key: str,
            require_backend: bool,
        ) -> bool:
            if str(projection.get(identity_key) or "").strip() != str(
                binding.get("id") or ""
            ).strip():
                # A context without the durable Session identity may carry a
                # deliberate per-run target (scheduled/CLI), not a stale Session
                # projection. The delivery backend must respect that target.
                return False
            if require_backend and not str(projection.get("agent_backend") or "").strip():
                return True
            for key in (
                "id",
                "agent_id",
                "agent_name",
                "agent_backend",
                "agent_variant",
                "model",
                "reasoning_effort",
            ):
                if key in projection and str(projection.get(key) or "").strip() != str(binding.get(key) or "").strip():
                    return True
            if "metadata" in projection and projection.get("metadata") != SessionTurnManager._binding_metadata(binding):
                return True
            return False

        session_target = spec.get("agent_session_target")
        if isinstance(session_target, dict) and differs(
            session_target,
            identity_key="id",
            require_backend=True,
        ):
            return True
        run_target = spec.get("agent_run_target")
        if isinstance(run_target, dict) and differs(
            run_target,
            identity_key="agent_session_id",
            require_backend=True,
        ):
            return True
        resolved_agent = spec.get("resolved_vibe_agent")
        if isinstance(resolved_agent, dict):
            for key, binding_key in (
                ("id", "agent_id"),
                ("name", "agent_name"),
                ("backend", "agent_backend"),
            ):
                if str(resolved_agent.get(key) or "").strip() != str(binding.get(binding_key) or "").strip():
                    return True
        if any(
            isinstance(projection, dict)
            and str(projection.get(identity_key) or "").strip() == str(binding.get("id") or "").strip()
            for projection, identity_key in (
                (session_target, "id"),
                (run_target, "agent_session_id"),
            )
        ):
            for key, binding_key in (
                ("vibe_agent_id", "agent_id"),
                ("vibe_agent_name", "agent_name"),
            ):
                if spec.get(key) and str(spec.get(key)).strip() != str(
                    binding.get(binding_key) or ""
                ).strip():
                    return True
        return False

    @staticmethod
    def _apply_session_binding_to_context(
        context: "MessageContext",
        binding: dict[str, Any],
    ) -> None:
        spec = dict(getattr(context, "platform_specific", None) or {})
        target = spec.get("agent_session_target")
        target = dict(target) if isinstance(target, dict) else {}
        target_id = str(target.get("id") or "").strip()
        if not target_id or target_id == str(binding.get("id") or "").strip():
            target.update(
                {
                    "id": binding.get("id"),
                    "agent_id": binding.get("agent_id"),
                    "agent_name": binding.get("agent_name"),
                    "agent_backend": binding.get("agent_backend"),
                    "agent_variant": binding.get("agent_variant"),
                    "model": binding.get("model"),
                    "reasoning_effort": binding.get("reasoning_effort"),
                    "metadata": SessionTurnManager._binding_metadata(binding),
                }
            )
        spec["agent_session_target"] = target
        # Keep every routing projection on the same durable winner. MessageHandler
        # and Controller prefer these cached fields over agent_session_target, so
        # leaving an old top-level value would route a newly bound turn elsewhere.
        agent_id = str(binding.get("agent_id") or "").strip()
        agent_name = str(binding.get("agent_name") or "").strip()
        spec["vibe_agent_id"] = agent_id or None
        spec["vibe_agent_name"] = agent_name or None
        if agent_id or agent_name:
            spec["resolved_vibe_agent"] = {
                "id": agent_id or None,
                "name": agent_name or None,
                "backend": binding.get("agent_backend"),
            }
        else:
            spec.pop("resolved_vibe_agent", None)
        run_target = spec.get("agent_run_target")
        if (
            isinstance(run_target, dict)
            and str(run_target.get("agent_session_id") or "").strip() == str(binding.get("id") or "").strip()
        ):
            run_target = dict(run_target)
            run_target.update(
                {
                    "agent_session_id": binding.get("id"),
                    "agent_id": binding.get("agent_id"),
                    "agent_name": binding.get("agent_name"),
                    "agent_backend": binding.get("agent_backend"),
                    "agent_variant": binding.get("agent_variant"),
                    "model": binding.get("model"),
                    "reasoning_effort": binding.get("reasoning_effort"),
                }
            )
            spec["agent_run_target"] = run_target
        context.platform_specific = spec

    @staticmethod
    def _session_binding_columns() -> tuple[Any, ...]:
        return (
            agent_sessions.c.id,
            agent_sessions.c.agent_id,
            agent_sessions.c.agent_name,
            agent_sessions.c.agent_backend,
            agent_sessions.c.agent_variant,
            agent_sessions.c.model,
            agent_sessions.c.reasoning_effort,
            agent_sessions.c.metadata_json,
            agent_sessions.c.status,
        )

    def _delivery_backend_in_transaction(
        self,
        conn: Connection,
        session_id: str,
        context: "MessageContext",
        *,
        resolved_agent: dict[str, str] | None = None,
        requested_backend: str | None = None,
    ) -> tuple[str, "MessageContext"]:
        """Resolve and persist a Session route while the caller owns the writer lock."""
        columns = self._session_binding_columns()
        binding = conn.execute(select(*columns).where(agent_sessions.c.id == session_id)).mappings().one_or_none()
        durable_backend = str((binding or {}).get("agent_backend") or "").strip()
        if durable_backend:
            if self._binding_projection_is_stale(context, dict(binding)):
                self._apply_session_binding_to_context(context, dict(binding))
            return durable_backend, context

        if resolved_agent is None:
            resolved_agent = self._context_vibe_agent_binding(context)
        requested_backend = requested_backend or (resolved_agent["agent_backend"] or self._context_backend(context))
        if not requested_backend:
            raise RuntimeError(f"Session {session_id} has no resolved backend")

        # An agentless Workbench Session is valid until its first turn resolves the
        # global default Agent. Materialize that decision before crossing the runtime
        # ownership gate; otherwise a queued Delivery is durable while its route is not.
        if binding is not None and binding["status"] == "active" and not str(binding["agent_backend"] or "").strip():
            values: dict[str, Any] = {
                "agent_backend": requested_backend,
                "updated_at": _utc_now_iso(),
            }
            if str(binding["agent_variant"] or "").strip() in {"", "default"}:
                values["agent_variant"] = requested_backend
            if resolved_agent["agent_id"] and not binding["agent_id"]:
                values["agent_id"] = resolved_agent["agent_id"]
            if resolved_agent["agent_name"] and not binding["agent_name"]:
                values["agent_name"] = resolved_agent["agent_name"]
            stored_metadata = self._binding_metadata(dict(binding))
            reconciled_metadata = reconcile_explicit_overrides(stored_metadata)
            if reconciled_metadata != stored_metadata:
                values["metadata_json"] = json.dumps(
                    reconciled_metadata,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
            conn.execute(
                update(agent_sessions)
                .where(agent_sessions.c.id == session_id)
                .where(agent_sessions.c.status == "active")
                .where(
                    or_(
                        agent_sessions.c.agent_backend == "",
                        agent_sessions.c.agent_backend.is_(None),
                    )
                )
                .values(**values)
            )
            binding = conn.execute(select(*columns).where(agent_sessions.c.id == session_id)).mappings().one_or_none()

        if binding is not None:
            binding_payload = dict(binding)
            self._apply_session_binding_to_context(context, binding_payload)
            durable_backend = str(binding_payload.get("agent_backend") or "").strip()
            if durable_backend:
                return durable_backend, context
            if binding_payload.get("status") == "active":
                raise RuntimeError(f"Session {session_id} has no durable backend binding")
        return requested_backend, context

    def _delivery_backend(
        self,
        session_id: str,
        context: Optional["MessageContext"],
    ) -> tuple[str, "MessageContext"]:
        resolved = context or self._delivery_context(session_id)
        columns = self._session_binding_columns()
        with self._sqlite_engine().connect() as conn:
            binding = conn.execute(select(*columns).where(agent_sessions.c.id == session_id)).mappings().one_or_none()
        durable_backend = str((binding or {}).get("agent_backend") or "").strip()
        if durable_backend:
            if self._binding_projection_is_stale(resolved, dict(binding)):
                self._apply_session_binding_to_context(resolved, dict(binding))
            return durable_backend, resolved

        resolved_agent = self._context_vibe_agent_binding(resolved)
        requested_backend = (
            resolved_agent["agent_backend"]
            or self._context_backend(resolved)
        )
        if not requested_backend:
            raise RuntimeError(f"Session {session_id} has no resolved backend")

        with self._sqlite_engine().begin() as conn:
            reserve_write_lock(conn)
            return self._delivery_backend_in_transaction(
                conn,
                session_id,
                resolved,
                resolved_agent=resolved_agent,
                requested_backend=requested_backend,
            )

    @contextmanager
    def _runtime_start_owner_for_binding(
        self,
        session_id: str,
        backend: str,
        binding: dict[str, Any] | None,
    ) -> Iterator[_RuntimeStartOwner]:
        """Hold the exact runtime generation through the owning SQLite commit."""
        durable_backend = str((binding or {}).get("agent_backend") or "").strip()
        requested_backend = str(backend or "").strip()
        session_anchor = str((binding or {}).get("session_anchor") or "").strip()
        workdir = (binding or {}).get("workdir")
        if binding is None or not durable_backend or not session_anchor:
            logger.warning(
                "Refusing runtime start with incomplete durable Session binding: session=%s",
                session_id,
            )
            yield _RuntimeStartOwner(
                session_id=session_id,
                backend=durable_backend or requested_backend,
                session_anchor=session_anchor,
                workdir=workdir,
                admitted=False,
            )
            return
        if requested_backend and durable_backend != requested_backend:
            logger.warning(
                "Refusing runtime start against stale Session backend: "
                "session=%s requested=%s durable=%s",
                session_id,
                requested_backend,
                durable_backend,
            )
            yield _RuntimeStartOwner(
                session_id=session_id,
                backend=durable_backend,
                session_anchor=session_anchor,
                workdir=workdir,
                admitted=False,
            )
            return
        service = getattr(self.controller, "agent_service", None)
        registry = getattr(service, "activation_registry", None) or getattr(
            self.controller,
            "runtime_activation",
            None,
        )
        if not isinstance(registry, RuntimeActivationRegistry):
            registry = None
        resolve = getattr(
            service,
            "runtime_activation_identity_for_session_binding",
            None,
        )
        resolution = RuntimeActivationResolution(authoritative=False)
        if callable(resolve):
            resolved = resolve(
                durable_backend,
                session_anchor=session_anchor,
                workdir=workdir,
            )
            if isinstance(resolved, RuntimeActivationResolution):
                resolution = resolved
        if registry is not None and not resolution.authoritative:
            logger.warning(
                "Refusing runtime start after inconclusive activation lookup: "
                "session=%s backend=%s",
                session_id,
                durable_backend,
            )
            yield _RuntimeStartOwner(
                session_id=session_id,
                backend=durable_backend,
                session_anchor=session_anchor,
                workdir=workdir,
                admitted=False,
            )
            return
        identity = resolution.identity
        if registry is None or identity is None:
            yield _RuntimeStartOwner(
                session_id=session_id,
                backend=durable_backend,
                session_anchor=session_anchor,
                workdir=workdir,
                admitted=True,
            )
            return
        hold = getattr(registry, "hold_if_current", None)
        if not callable(hold):
            yield _RuntimeStartOwner(
                session_id=session_id,
                backend=durable_backend,
                session_anchor=session_anchor,
                workdir=workdir,
                admitted=False,
                identity=identity,
            )
            return
        with hold(identity) as admitted:
            yield _RuntimeStartOwner(
                session_id=session_id,
                backend=durable_backend,
                session_anchor=session_anchor,
                workdir=workdir,
                admitted=bool(admitted),
                identity=identity,
            )

    @contextmanager
    def _runtime_start_owner(
        self,
        session_id: str,
        backend: str,
    ) -> Iterator[_RuntimeStartOwner]:
        with self._sqlite_engine().connect() as conn:
            binding = (
                conn.execute(
                    select(
                        agent_sessions.c.agent_backend,
                        agent_sessions.c.session_anchor,
                        agent_sessions.c.workdir,
                    ).where(agent_sessions.c.id == session_id)
                )
                .mappings()
                .one_or_none()
            )
        with self._runtime_start_owner_for_binding(
            session_id, backend, dict(binding) if binding is not None else None
        ) as owner:
            yield owner

    @contextmanager
    def _runtime_start_owner_in_transaction(
        self,
        conn: Connection,
        session_id: str,
        backend: str,
    ) -> Iterator[_RuntimeStartOwner]:
        binding = (
            conn.execute(
                select(
                    agent_sessions.c.agent_backend,
                    agent_sessions.c.session_anchor,
                    agent_sessions.c.workdir,
                ).where(agent_sessions.c.id == session_id)
            )
            .mappings()
            .one_or_none()
        )
        with self._runtime_start_owner_for_binding(
            session_id, backend, dict(binding) if binding is not None else None
        ) as owner:
            yield owner

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
            message_kind=request.message_kind,
        )

    def _request_from_delivery(
        self,
        delivery: dict[str, Any],
        *,
        has_attachments: bool = False,
    ) -> DeliveryRequest:
        payload = delivery_store.delivery_payload(delivery)
        return DeliveryRequest(
            session_id=str(delivery["session_id"]),
            priority=str(delivery["priority"]),
            content=str(delivery.get("dispatch_text") or ""),
            has_content=delivery_store.has_substantive_content(
                delivery,
                has_attachments=has_attachments,
            ),
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
            message_kind=str(payload.get("message_kind") or "unknown"),
        )

    def _committed_delivery_result(
        self,
        delivery_id: str,
        *,
        attempted_turn_id: str | None = None,
        reason: str | None = None,
    ) -> DeliveryResult:
        """Return the exact post-transition Delivery instead of a cached claim."""

        with self._sqlite_engine().connect() as conn:
            delivery = delivery_store.get_delivery(conn, delivery_id)
        if delivery is None:
            raise RuntimeError(f"durable Delivery disappeared after transition: {delivery_id}")
        turn_id = str(
            delivery.get("turn_id")
            or delivery.get("current_target_turn_id")
            or attempted_turn_id
            or ""
        ).strip()
        state = str(delivery["state"])
        return DeliveryResult(
            delivery_id,
            str(delivery.get("message_id") or "").strip() or None,
            state,
            turn_id or None,
            reason,
            # Every caller reaches here right after dispatching a Turn this
            # Delivery participates in, so an owned state means this input
            # started the work rather than joining a Turn already running.
            admission="started" if state in {"claimed", "accepted"} else "",
        )

    @classmethod
    def _claim_start_batch(
        cls,
        conn: Connection,
        *,
        owner: _RuntimeStartOwner,
        turn_id: str,
        session_id: str,
        backend: str,
        deliveries: list[dict[str, Any]],
        dispatch_text: str,
        attempt_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Claim a Delivery batch and all linked Agent Runs atomically."""

        if owner.session_id != session_id:
            raise RuntimeError("runtime start owner does not match Delivery claim target")
        binding = conn.execute(
            select(
                agent_sessions.c.agent_backend,
                agent_sessions.c.session_anchor,
                agent_sessions.c.workdir,
            ).where(agent_sessions.c.id == session_id)
        ).mappings().one_or_none()
        binding_current = bool(
            binding is not None
            and str(binding["agent_backend"] or "").strip() == owner.backend
            and owner.backend == str(backend or "").strip()
            and str(binding["session_anchor"] or "").strip() == owner.session_anchor
            and binding["workdir"] == owner.workdir
        )
        if not owner.admitted or not binding_current:
            for delivery in deliveries:
                if delivery.get("state") != "reserved":
                    continue
                queued = delivery_store.cas_delivery(
                    conn,
                    str(delivery["id"]),
                    expected_version=int(delivery["version"]),
                    expected_states=("reserved",),
                    values={"priority": "p3", "state": "queued"},
                    history_event={
                        "kind": "admission",
                        "turn_id": turn_id,
                        "outcome": "runtime_generation_retired_before_claim",
                    },
                )
                if queued is None:
                    raise RuntimeError("runtime-rejected Delivery queue fallback lost")
            return None

        denied_remote = [
            (delivery, reason)
            for delivery in deliveries
            if (reason := cls._remote_delivery_execution_denial(conn, delivery)) is not None
        ]
        if denied_remote:
            for delivery, reason in denied_remote:
                if not cls._retire_delivery_not_written(
                    conn,
                    session_id,
                    str(delivery["id"]),
                    reason=reason,
                ):
                    raise RuntimeError("remote Delivery authorization retirement lost")
                logger.warning(
                    "retired remote-origin Delivery=%s before Agent dispatch: %s",
                    delivery["id"],
                    reason,
                )
            return None

        unstartable = [
            delivery
            for delivery in deliveries
            if not cls._delivery_agent_runs_can_start(conn, delivery)
        ]
        if unstartable:
            cls._retire_unstartable_deliveries(conn, session_id, unstartable)
            return None

        run_ids = list(
            dict.fromkeys(
                run_id
                for delivery in deliveries
                for run_id in delivery_store.agent_run_ids_for_delivery(conn, delivery)
            )
        )
        if run_ids:
            from storage.background import claim_agent_runs_for_turn_in_connection

            claimed_run_ids = claim_agent_runs_for_turn_in_connection(conn, run_ids)
            if claimed_run_ids != run_ids:
                raise RuntimeError(
                    "Delivery batch contains an unclaimable Agent Run: "
                    f"session={session_id} deliveries="
                    f"{','.join(str(delivery.get('id')) for delivery in deliveries)} "
                    f"runs={','.join(run_ids)}"
                )
        return delivery_store.claim_start_batch(
            conn,
            turn_id=turn_id,
            session_id=session_id,
            backend=backend,
            deliveries=deliveries,
            dispatch_text=dispatch_text,
            attempt_id=attempt_id,
        )

    @staticmethod
    def _remote_delivery_execution_denial(
        conn: Connection,
        delivery: dict[str, Any],
    ) -> str | None:
        """Recheck deferred remote chat authority immediately before execution."""

        if not delivery_store.delivery_has_remote_resource_context(delivery):
            return None

        from core.services import sessions as workbench_sessions_service
        from core.vibe_agents import ensure_session_agent_access
        from storage import project_access_service, resource_access_service

        metadata = delivery_store.delivery_payload(delivery).get("metadata")
        try:
            context = resource_access_service.resource_user_context_from_metadata(metadata)
        except resource_access_service.ResourceAccessError as error:
            return error.code
        if context is None or not context.can_chat:
            return "remote_chat_access_forbidden"
        if not project_access_service.role_allows(
            project_access_service.get_effective_session_role(
                conn,
                context,
                str(delivery["session_id"]),
            ),
            "editor",
        ):
            return "remote_project_access_forbidden"
        try:
            session = workbench_sessions_service.get_session(
                conn,
                str(delivery["session_id"]),
                authorization_context=context,
            )
            ensure_session_agent_access(conn, session, user_context=context)
        except LookupError:
            return "remote_session_or_agent_not_found"
        except PermissionError:
            return "remote_agent_access_forbidden"
        return None

    @staticmethod
    def _delivery_agent_runs_can_start(
        conn: Connection,
        delivery: dict[str, Any],
    ) -> bool:
        """Whether a Delivery's linked Agent Runs can still be claimed.

        A Run that already settled terminally (or asked to cancel) leaves its
        Delivery permanently unexecutable: every claim fails the Agent Run guard,
        and because recovery drains the same queue on startup, one such row turns
        into a controller crash loop. ``_claim_start_batch`` retires it instead.
        """

        from storage.background import inspect_agent_runs_for_turn_in_connection

        run_ids = delivery_store.agent_run_ids_for_delivery(conn, delivery)
        if not run_ids:
            return True
        eligible_run_ids, stale_run_ids = inspect_agent_runs_for_turn_in_connection(
            conn, run_ids
        )
        return not stale_run_ids and eligible_run_ids == run_ids

    @classmethod
    def _retire_unstartable_deliveries(
        cls,
        conn: Connection,
        session_id: str,
        deliveries: list[dict[str, Any]],
    ) -> None:
        """Retire Deliveries no Agent Run will ever execute, in the claim path.

        Every start claim funnels through ``_claim_start_batch`` — startup drain,
        live FIFO drain, explicit queue promotion, immediate admission — so
        retiring here is what keeps one poisoned row from bricking any of them.
        """

        for delivery in deliveries:
            if not cls._retire_delivery_not_written(
                conn,
                session_id,
                str(delivery["id"]),
                reason="terminal_agent_run_before_start_claim",
            ):
                raise RuntimeError(
                    "unstartable Delivery retirement lost: "
                    f"session={session_id} delivery={delivery['id']}"
                )
            logger.warning(
                "retired Delivery=%s because its Agent Run can no longer start",
                delivery["id"],
            )

    @staticmethod
    def _start_claim_retired_rows(
        conn: Connection,
        deliveries: list[dict[str, Any]],
    ) -> bool:
        """Whether a refused start claim retired rows, so the queue moved on."""

        for delivery in deliveries:
            row = delivery_store.get_delivery(conn, str(delivery["id"]))
            if row is not None and str(row["state"]) == "retired":
                return True
        return False

    @staticmethod
    def _cancel_runs_for_retired_delivery(
        conn: Connection,
        delivery: dict[str, Any] | None,
    ) -> list[str]:
        """Make Delivery retirement and exact Run cancellation one transaction."""

        if delivery is None or delivery.get("state") != "retired":
            return []
        from storage.background import (
            cancel_agent_runs_for_retired_deliveries_in_connection,
        )

        return cancel_agent_runs_for_retired_deliveries_in_connection(
            conn,
            session_id=str(delivery["session_id"]),
            delivery_ids=[str(delivery["id"])],
        )

    @classmethod
    def _record_definitive_delivery_attempt(
        cls,
        conn: Connection,
        delivery_id: str,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        delivery = delivery_store.record_definitive_attempt(
            conn,
            delivery_id,
            **kwargs,
        )
        cls._cancel_runs_for_retired_delivery(conn, delivery)
        return delivery

    @classmethod
    def _terminalize_detached_run_replacement(
        cls,
        conn: Connection,
        *,
        run_id: str,
        session_id: str,
        current: dict[str, Any],
    ) -> bool:
        """Give the Turn owner sole authority to terminalize a canceled successor."""

        if current.get("control_mode") != "replace" or current.get(
            "control_state"
        ) not in {
            "pending",
            "interrupting",
            "waiting_terminal",
            "reconciling",
        }:
            return False
        run = conn.execute(
            select(agent_runs.c.status, agent_runs.c.delivery_id)
            .where(agent_runs.c.id == run_id)
            .where(agent_runs.c.session_id == session_id)
            .limit(1)
        ).mappings().first()
        successor_delivery_id = str(
            current.get("control_successor_delivery_id") or ""
        )
        successor_turn_id = str(current.get("control_successor_turn_id") or "")
        if (
            run is None
            or normalize_run_status(run["status"]) not in {"queued", "running"}
            or str(run["delivery_id"] or "") != successor_delivery_id
            or not successor_turn_id
        ):
            return False
        successor = delivery_store.get_turn(conn, successor_turn_id)
        successor_delivery = delivery_store.get_delivery(
            conn,
            successor_delivery_id,
        )
        if (
            successor is None
            or successor["session_id"] != session_id
            or successor["state"] != "waiting"
            or successor["initial_delivery_id"] != successor_delivery_id
            or successor_delivery is None
            or successor_delivery["session_id"] != session_id
            or successor_delivery["state"] != "interrupt_waiting"
            or successor_delivery["turn_id"] != successor_turn_id
            or successor_delivery["turn_role"] != "initial"
        ):
            return False
        terminalized = cls._write_terminal_snapshot(
            conn,
            successor_turn_id,
            outcome="not_written",
            settled_by="agent_run_canceled",
            evidence_kind="replacement_run_canceled",
        )
        if not terminalized.get("changed"):
            raise RuntimeError("replacement Run cancellation lost Turn authority")
        return True

    @classmethod
    def _retire_delivery_not_written(
        cls,
        conn: Connection,
        session_id: str,
        delivery_id: str,
        *,
        reason: str,
    ) -> bool:
        retired = delivery_store.retire_not_written(
            conn,
            session_id,
            delivery_id,
            reason=reason,
        )
        if retired:
            cls._cancel_runs_for_retired_delivery(
                conn,
                delivery_store.get_delivery(conn, delivery_id),
            )
        return retired

    def _claim_fifo_batch_in_transaction(
        self,
        conn: Connection,
        *,
        owner: _RuntimeStartOwner | None,
        session_id: str,
        backend: str,
        expected_head_id: str | None = None,
        expected_head_version: int | None = None,
        owner_factory: Callable[[Connection, list[dict[str, Any]]], ContextManager[_RuntimeStartOwner]] | None = None,
    ) -> str | None:
        """Claim the exact open FIFO head while the caller owns SQLite's writer slot."""

        if owner is None and owner_factory is None:
            raise ValueError("FIFO claim requires a runtime owner or owner factory")

        if (
            delivery_store.active_turn(conn, session_id) is not None
            or backend in self._draining_backends
        ):
            return None
        while True:
            head = delivery_store.claimable_fifo_head(conn, session_id)
            queued = delivery_store.claimable_fifo_prefix(conn, session_id)
            if head is None or not queued or str(queued[0]["id"]) != str(head["id"]):
                return None
            if expected_head_id is not None and (
                str(head["id"]) != expected_head_id
                or int(head["version"]) != expected_head_version
            ):
                return None
            segment_payloads = _collect_delivery_segment(queued)
            segment = [
                delivery_store.get_delivery(conn, str(row["id"]))
                for row in segment_payloads
            ]
            if not segment or any(row is None for row in segment):
                return None
            delivery_rows = [row for row in segment if row is not None]
            invalid_rows = [
                row
                for row in delivery_rows
                if not self._has_resolvable_delivery_input(conn, row)
            ]
            if invalid_rows:
                for row in invalid_rows:
                    if not self._retire_delivery_not_written(
                        conn,
                        session_id,
                        str(row["id"]),
                        reason="invalid_input_before_fifo_claim",
                    ):
                        raise RuntimeError("invalid FIFO Delivery retirement lost")
                    logger.warning(
                        "retired queued Delivery=%s because it has no resolvable input",
                        row["id"],
                    )
                continue
            unstartable_rows = [row for row in delivery_rows if not self._delivery_agent_runs_can_start(conn, row)]
            if unstartable_rows:
                self._retire_unstartable_deliveries(
                    conn,
                    session_id,
                    unstartable_rows,
                )
                continue
            turn_id = delivery_store.new_turn_id()
            dispatch_text = _segment_dispatch_text(segment_payloads)
            if owner is not None:
                claimed = self._claim_start_batch(
                    conn,
                    owner=owner,
                    turn_id=turn_id,
                    session_id=session_id,
                    backend=backend,
                    deliveries=delivery_rows,
                    dispatch_text=dispatch_text,
                )
            else:
                assert owner_factory is not None
                with owner_factory(conn, delivery_rows) as prepared_owner:
                    prepared_backend = prepared_owner.backend or backend
                    if prepared_backend in self._draining_backends:
                        self._deferred_restart_sessions.setdefault(
                            prepared_backend,
                            set(),
                        ).add(session_id)
                        return None
                    claimed = self._claim_start_batch(
                        conn,
                        owner=prepared_owner,
                        turn_id=turn_id,
                        session_id=session_id,
                        backend=prepared_backend,
                        deliveries=delivery_rows,
                        dispatch_text=dispatch_text,
                    )
            if claimed is not None:
                return turn_id
            if self._start_claim_retired_rows(conn, delivery_rows):
                continue
            return None

    def _hydrate_delivery_context(
        self,
        context: "MessageContext",
        delivery: dict[str, Any],
    ) -> dict[str, Any]:
        """Restore dispatch inputs only from the durable Delivery snapshot."""

        payload = delivery_store.delivery_payload(delivery)
        context.platform = str(payload.get("platform") or context.platform or "avibe")
        native_message_id = str(payload.get("native_message_id") or "").strip()
        context.message_id = (
            native_message_id
            if context.platform != "avibe" and native_message_id
            else str(delivery["id"])
        )
        if context.platform_specific is None:
            context.platform_specific = {}
        metadata = payload.get("metadata") or {}
        raw_snapshot = delivery.get("snapshot_json")
        try:
            snapshot = json.loads(raw_snapshot) if isinstance(raw_snapshot, str) else {}
        except (TypeError, ValueError):
            snapshot = {}
        legacy_workbench = context.platform == "avibe" and (
            not isinstance(snapshot, dict) or "message_kind" not in snapshot
        )
        author_id = payload.get("author_id")
        if legacy_workbench:
            author_id = delivery_store.legacy_admitted_user_id(metadata)
        if author_id:
            context.user_id = str(author_id)
        context.message_kind = normalize_message_kind(payload.get("message_kind"))
        context.is_original_human_text = context.message_kind == "original"
        memory_enabled = bool(
            getattr(
                getattr(getattr(self.controller, "config", None), "memory", None),
                "enabled",
                False,
            )
        )
        memory_cli_admitted = bool(
            context.platform == "avibe"
            and memory_enabled
            and author_id
            and (
                not legacy_workbench
                or delivery_store.legacy_is_cli_admitted(metadata)
            )
        )
        if memory_cli_admitted:
            context.platform_specific["memory_cli_admitted"] = True
        else:
            context.platform_specific.pop("memory_cli_admitted", None)
        context.platform_specific.update(
            {
                "delivery_id": str(delivery["id"]),
                "scope_id": payload.get("scope_id"),
                "display_text": payload.get("text") or "",
                "message_content": dict(payload.get("content") or {}),
                "message_metadata": dict(metadata),
                "author_id": author_id,
                "author_name": payload.get("author_name"),
                "native_message_id": payload.get("native_message_id"),
                "message_kind": context.message_kind,
                "delivery_admission_context": (
                    delivery_store.delivery_admission_context(delivery)
                ),
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
        context.is_original_human_attachment = bool(
            context.message_kind == "original" and context.files
        )
        return payload

    @staticmethod
    def _lifecycle_anchor_for_delivery(
        delivery: dict[str, Any],
        session_id: str,
    ) -> str:
        """Use the same raw anchor that capture and `/new` use for this turn."""

        admission_context = delivery_store.delivery_admission_context(delivery)
        route = (
            admission_context.get("message_handler_route")
            if isinstance(admission_context, dict)
            else None
        )
        if isinstance(route, dict):
            raw_session_id = route.get("base_session_id")
            if isinstance(raw_session_id, str) and raw_session_id:
                return raw_session_id
        return session_id

    @staticmethod
    def _has_resolvable_delivery_input(
        conn: Connection,
        delivery: dict[str, Any],
    ) -> bool:
        payload = delivery_store.delivery_payload(delivery)
        from core.workbench_media import resolve_attachment_specs

        specs = resolve_attachment_specs(
            conn,
            session_id=str(delivery["session_id"]),
            attachments=(payload.get("content") or {}).get("attachments") or [],
        )
        return delivery_store.has_substantive_content(
            delivery,
            has_attachments=bool(specs),
        )

    @staticmethod
    def _delivery_has_attachment_references(delivery: dict[str, Any]) -> bool:
        payload = delivery_store.delivery_payload(delivery)
        return bool((payload.get("content") or {}).get("attachments"))

    def _hydrate_delivery_batch_context(
        self,
        context: "MessageContext",
        deliveries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not deliveries:
            raise RuntimeError("a durable Turn has no initial Delivery batch")
        first = self._hydrate_delivery_context(context, deliveries[0])
        payloads = [delivery_store.delivery_payload(row) for row in deliveries]
        attachments = [
            attachment
            for payload in payloads
            for attachment in ((payload.get("content") or {}).get("attachments") or [])
        ]
        from core.workbench_media import (
            file_attachments_from_specs,
            resolve_attachment_specs,
        )

        with self._sqlite_engine().connect() as conn:
            specs = resolve_attachment_specs(
                conn,
                session_id=str(deliveries[0]["session_id"]),
                attachments=attachments,
            )
            run_ids = list(
                dict.fromkeys(
                    run_id
                    for row in deliveries
                    for run_id in delivery_store.agent_run_ids_for_delivery(conn, row)
                )
            )
        context.files = file_attachments_from_specs(specs)
        context.platform_specific.update(
            {
                "delivery_id": str(deliveries[0]["id"]),
                "delivery_ids": [str(row["id"]) for row in deliveries],
                # Every reaction target this Turn absorbed. Only the first
                # Delivery hydrates the dispatch context, so without this the
                # admission receipts of the merged rest are never cleared.
                "delivery_ack_targets": [
                    target
                    for target in (
                        self._delivery_ack_target(row) for row in deliveries
                    )
                    if target
                ],
                # Every display snapshot this Turn dispatches, in FIFO order.
                # ``_hydrate_delivery_context`` set the singular ``display_text`` from
                # the FIRST Delivery only, while ``_segment_dispatch_text`` sends the
                # whole merged batch to the backend: a consumer that shows one prompt
                # (the IM prompt echo) would announce one instruction for a result that
                # answers several. Snapshots, never ``dispatch_text`` — the replay
                # guards prepended there are backend-only.
                "display_texts": [
                    str(payload.get("text") or "")
                    for payload in payloads
                    if str(payload.get("text") or "").strip()
                ],
                # Same reason, one layer in: for the kinds whose prompt Avibe COMPOSES
                # (watch / webhook / hook) the snapshot above holds the generated
                # evidence too, so the echo shows the definition's stored instruction
                # instead — and an instruction edited between two firings leaves a
                # merged batch dispatching two different ones. Each Delivery's own
                # stamped instruction, from its captured provenance.
                "harness_display_prompts": [
                    instruction
                    for instruction in (
                        str(
                            (
                                (_scheduled_provenance(payload) or {}).get(
                                    "platform_specific"
                                )
                                or {}
                            ).get("harness_display_prompt")
                            or ""
                        ).strip()
                        for payload in payloads
                    )
                    if instruction
                ],
                "message_content": {
                    "text": "\n".join(
                        str(payload.get("text") or "")
                        for payload in payloads
                        if str(payload.get("text") or "")
                    ),
                    **({"attachments": attachments} if attachments else {}),
                },
            }
        )
        self._append_accepted_agent_run_ids(context.platform_specific, run_ids)
        return first

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
        dedupe_key = delivery_store.native_dedupe_key(
            request.platform,
            request.native_message_id,
            scope_id=request.scope_id,
        )
        if dedupe_key:
            existing = delivery_store.get_delivery_by_native_identity(
                conn,
                platform=request.platform,
                native_message_id=str(request.native_message_id or ""),
                scope_id=request.scope_id,
                session_id=request.session_id,
                normalize_legacy=True,
            )
            if existing is not None:
                if existing["session_id"] != request.session_id:
                    raise ValueError("Delivery dedupe identity belongs to another Session")
                return existing
        return delivery_store.insert_delivery(
            conn,
            delivery_id=delivery_id,
            session_id=request.session_id,
            priority=priority,
            state=state,
            snapshot=SessionTurnManager._delivery_snapshot(request),
            dispatch_text=str(request.content or ""),
            dedupe_key=dedupe_key,
            history_event={
                "kind": "admission",
                "priority": priority,
                "state": state,
                **(
                    {"context": dict(request.admission_context)}
                    if request.admission_context
                    else {}
                ),
            },
        )

    @staticmethod
    def reserve_delivery(
        conn: Connection,
        request: DeliveryRequest,
    ) -> dict[str, Any]:
        """Persist a producer-owned content reservation in the caller's transaction."""

        if request.content is None:
            raise ValueError("content Delivery requires a Message snapshot")
        if request.priority not in {"p1", "p3"}:
            raise ValueError("content reservation requires P1 or P3 priority")
        return SessionTurnManager._insert_delivery(
            conn,
            request,
            priority=request.priority,
            state="reserved",
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

    async def _dispatch_steer_batch(
        self,
        backend: str,
        deliveries: list[dict[str, Any]],
        *,
        logical_turn_id: str,
        native_turn_id: str,
        attempt_id: str,
        context: "MessageContext",
    ) -> DeliveryResult:
        delivery_id = str(deliveries[0]["id"])
        try:
            metadata = await self._steer_input_metadata(deliveries)
            request = SteerRequest(
                target_session_id=str(deliveries[0]["session_id"]),
                expected_logical_turn_id=logical_turn_id,
                expected_native_turn_id=native_turn_id,
                text=_segment_dispatch_text(deliveries),
                attempt_id=attempt_id,
                input_metadata=metadata,
            )
        except asyncio.CancelledError:
            await self._finish_steer(
                delivery_id,
                steer_result(SteerOutcome.REFUSED, reason="preparation_cancelled"),
                context=context,
            )
            raise
        except Exception as exc:
            logger.exception("steering preparation failed before native write for delivery=%s", delivery_id)
            receipt = steer_result(
                SteerOutcome.REFUSED,
                reason="preparation_failed",
                error_type=type(exc).__name__,
            )
        else:
            receipt = await self._attempt_steer(backend, request)
        return await self._finish_steer(delivery_id, receipt, context=context)

    async def _reconcile_steer_attempt(
        self,
        backend: str,
        request: SteerReconcileRequest,
    ):
        try:
            return await reconcile_steer_attempt(self.controller, backend, request)
        except Exception as exc:
            logger.exception(
                "native steering reconciliation failed for Session=%s Turn=%s Attempt=%s",
                request.target_session_id,
                request.expected_logical_turn_id,
                request.attempt_id,
            )
            return steer_result(
                SteerOutcome.UNKNOWN,
                reason="adapter_reconciliation_error",
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
        backend, resolved_context = self._delivery_backend(
            request.session_id,
            context,
        )
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
        with self._runtime_start_owner(request.session_id, backend) as start_owner, self._sqlite_engine().begin() as conn:
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
                if existing is not None and existing["state"] in {"reserved", "queued"}:
                    self._retire_delivery_not_written(
                        conn,
                        request.session_id,
                        str(existing["id"]),
                        reason="session_archived",
                    )
                    return DeliveryResult(str(existing["id"]), None, "retired")
                raise ValueError("Session is archived")
            delivery = self._insert_delivery(
                conn,
                request,
                priority="p3",
                state="queued",
            )
            if delivery["state"] not in {"queued", "reserved"}:
                return DeliveryResult(
                    str(delivery["id"]),
                    str(delivery.get("message_id") or "") or None,
                    str(delivery["state"]),
                    str(delivery.get("turn_id") or delivery.get("current_target_turn_id") or "")
                    or None,
                )
            if delivery["state"] == "reserved":
                queued = delivery_store.cas_delivery(
                    conn,
                    str(delivery["id"]),
                    expected_version=int(delivery["version"]),
                    expected_states=("reserved",),
                    values={"state": "queued"},
                    history_event={"kind": "queue", "reason": "p3_admission"},
                )
                if queued is None:
                    raise RuntimeError("P3 queue claim lost after writer reservation")
                delivery = queued
            active = delivery_store.active_turn(conn, request.session_id)
            while active is None and not backend_draining and not request.admission_only:
                queued_payloads = delivery_store.claimable_fifo_prefix(
                    conn, request.session_id
                )
                segment_payloads = _collect_delivery_segment(queued_payloads)
                segment = [
                    delivery_store.get_delivery(conn, str(row["id"]))
                    for row in segment_payloads
                ]
                if not segment or any(row is None for row in segment):
                    break
                delivery_rows = [row for row in segment if row is not None]
                turn_id = delivery_store.new_turn_id()
                claimed_batch = self._claim_start_batch(
                    conn,
                    owner=start_owner,
                    turn_id=turn_id,
                    session_id=request.session_id,
                    backend=backend,
                    deliveries=delivery_rows,
                    dispatch_text=_segment_dispatch_text(segment_payloads),
                )
                if claimed_batch is None:
                    turn_id = None
                    # The claim path retired rows no Agent Run can execute; this
                    # admission still deserves its turn, so re-derive and retry.
                    if not self._start_claim_retired_rows(conn, delivery_rows):
                        break
                    delivery = (
                        delivery_store.get_delivery(conn, str(delivery["id"])) or delivery
                    )
                    if delivery["state"] != "queued":
                        break
                    continue
                for claimed in claimed_batch.get("deliveries", []):
                    if str(claimed["id"]) == str(delivery["id"]):
                        delivery = claimed
                        delivery_turn_id = turn_id
                        if int(claimed.get("turn_position") or 0) == 0:
                            start_context = context
                        break
                break
        if backend_draining:
            self._deferred_restart_sessions.setdefault(backend, set()).add(
                request.session_id
            )
        if turn_id:
            await self._start_persisted_turn(turn_id, context=start_context)
            return self._committed_delivery_result(
                str(delivery["id"]),
                attempted_turn_id=turn_id,
            )
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
            return await self._promote_fifo_head(
                request.session_id,
                backend,
                context,
                expected_delivery_id=request.expected_delivery_id,
            )

        observed, identity = self._observe_active_delivery_turn(request.session_id)
        observed_id = str((observed or {}).get("id") or "") or None
        attempt_id: str | None = None
        native_id: str | None = None
        turn_id: str | None = None
        steer_backend = backend
        delivery: dict[str, Any]
        with self._runtime_start_owner(request.session_id, backend) as start_owner, self._sqlite_engine().begin() as conn:
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
                    self._retire_delivery_not_written(
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
            if current is not None and self._delivery_has_attachment_references(delivery):
                claimed = delivery_store.cas_delivery(
                    conn,
                    str(delivery["id"]),
                    expected_version=int(delivery["version"]),
                    expected_states=("reserved",),
                    values={"priority": "p3", "state": "queued"},
                    history_event={
                        "kind": "steer",
                        "turn_id": str(current["id"]),
                        "outcome": "attachments_require_new_turn",
                    },
                )
                if claimed is None:
                    raise RuntimeError("attachment P1 fallback claim lost")
                delivery = claimed
            elif same_active:
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
                claimed_batch = self._claim_start_batch(
                    conn,
                    owner=start_owner,
                    turn_id=turn_id,
                    session_id=request.session_id,
                    backend=backend,
                    deliveries=[delivery],
                    dispatch_text=str(delivery.get("dispatch_text") or ""),
                )
                if claimed_batch is None:
                    delivery = delivery_store.get_delivery(conn, str(delivery["id"])) or delivery
                    turn_id = None
                else:
                    delivery = claimed_batch["deliveries"][0]
            elif observed_id and str(current["id"]) == observed_id:
                attempt_id = delivery_store.new_attempt_id()
                pending_rows = delivery_store.open_pending_steer_batch(
                    conn,
                    deliveries=[delivery],
                    turn_id=str(current["id"]),
                    attempt_id=attempt_id,
                )
                delivery = pending_rows[0]
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

        if delivery["state"] == "claimed" and turn_id:
            await self._start_persisted_turn(turn_id, context=context)
            return self._committed_delivery_result(
                str(delivery["id"]),
                attempted_turn_id=turn_id,
            )
        elif delivery["state"] == "steering" and turn_id and attempt_id and native_id:
            return await self._dispatch_steer_batch(
                steer_backend,
                [delivery],
                logical_turn_id=turn_id,
                native_turn_id=native_id,
                attempt_id=attempt_id,
                context=context,
            )
        return DeliveryResult(str(delivery["id"]), None, str(delivery["state"]), turn_id)

    async def _promote_fifo_head(
        self,
        session_id: str,
        backend: str,
        context: "MessageContext",
        *,
        expected_delivery_id: str | None = None,
    ) -> DeliveryResult:
        with self._sqlite_engine().connect() as conn:
            observed_head = delivery_store.ordering_head(conn, session_id)
            observed_segment_payloads = _collect_delivery_segment(
                delivery_store.claimable_fifo_prefix(conn, session_id)
            )
        if observed_head is None:
            return DeliveryResult(None, None, "empty")
        observed_owner_id = str(observed_head["id"])
        if expected_delivery_id and observed_owner_id != expected_delivery_id:
            return DeliveryResult(
                expected_delivery_id,
                None,
                "refused",
                reason="stale_head",
            )
        if observed_head["state"] != "queued":
            return DeliveryResult(
                observed_owner_id,
                None,
                "refused",
                reason="ordering_fence",
            )
        observed_turn, identity = self._observe_active_delivery_turn(session_id)
        observed_turn_id = str((observed_turn or {}).get("id") or "") or None
        observed_segment_ids = [str(row["id"]) for row in observed_segment_payloads]
        if not observed_segment_ids or observed_segment_ids[0] != observed_owner_id:
            return DeliveryResult(observed_owner_id, None, "refused", reason="stale_head")
        delivery_id = observed_owner_id
        turn_id: str | None = None
        attempt_id: str | None = None
        native_id: str | None = None
        steer_backend = backend
        claimed_rows: list[dict[str, Any]] = []
        dispatch_text = _segment_dispatch_text(observed_segment_payloads)
        with self._runtime_start_owner(session_id, backend) as start_owner, self._sqlite_engine().begin() as conn:
            reserve_write_lock(conn)
            status = conn.execute(
                select(agent_sessions.c.status).where(agent_sessions.c.id == session_id)
            ).scalar_one_or_none()
            if status != "active":
                return DeliveryResult(observed_owner_id, None, "refused", reason="session_archived")
            current_head = delivery_store.ordering_head(conn, session_id)
            if (
                current_head is None
                or str(current_head["id"]) != observed_owner_id
                or (
                    expected_delivery_id
                    and str(current_head["id"]) != expected_delivery_id
                )
            ):
                return DeliveryResult(
                    delivery_id,
                    None,
                    "refused",
                    reason="stale_head",
                )
            if current_head["state"] != "queued":
                return DeliveryResult(delivery_id, None, "refused", reason="ordering_fence")
            current_segment_payloads = _collect_delivery_segment(
                delivery_store.claimable_fifo_prefix(conn, session_id)
            )
            if [str(row["id"]) for row in current_segment_payloads[: len(observed_segment_ids)]] != observed_segment_ids:
                return DeliveryResult(delivery_id, None, "refused", reason="stale_head")
            segment = [
                delivery_store.get_delivery(conn, candidate_id)
                for candidate_id in observed_segment_ids
            ]
            if any(row is None or row["state"] != "queued" for row in segment):
                return DeliveryResult(delivery_id, None, "refused", reason="stale_head")
            delivery_rows = [row for row in segment if row is not None]
            current_turn = delivery_store.active_turn(conn, session_id)
            if current_turn is None:
                turn_id = delivery_store.new_turn_id()
                claimed = self._claim_start_batch(
                    conn,
                    owner=start_owner,
                    turn_id=turn_id,
                    session_id=session_id,
                    backend=backend,
                    deliveries=delivery_rows,
                    dispatch_text=dispatch_text,
                )
                if claimed is None:
                    turn_id = None
                    # The claim path retired rows no Agent Run can execute, so the
                    # promoted segment no longer exists as the caller observed it.
                    if self._start_claim_retired_rows(conn, delivery_rows):
                        return DeliveryResult(
                            delivery_id,
                            None,
                            "refused",
                            reason="stale_head",
                        )
                else:
                    claimed_rows = claimed["deliveries"]
            elif any(
                self._delivery_has_attachment_references(row)
                for row in delivery_rows
            ):
                return DeliveryResult(
                    delivery_id,
                    None,
                    "queued",
                    str(current_turn["id"]),
                    reason="attachments_wait_for_new_turn",
                )
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
                claimed_rows = delivery_store.open_steer_attempt_batch(
                    conn,
                    deliveries=delivery_rows,
                    turn_id=turn_id,
                    attempt_id=attempt_id,
                    expected_native_turn_id=native_id,
                )
            elif observed_turn_id and str(current_turn["id"]) == observed_turn_id:
                turn_id = str(current_turn["id"])
                attempt_id = delivery_store.new_attempt_id()
                claimed_rows = delivery_store.open_pending_steer_batch(
                    conn,
                    deliveries=delivery_rows,
                    turn_id=turn_id,
                    attempt_id=attempt_id,
                )
            else:
                return DeliveryResult(
                    delivery_id,
                    None,
                    "refused",
                    reason="stale_turn",
                )
            if not claimed_rows:
                return DeliveryResult(delivery_id, None, "refused", reason="claim_lost")

        leader = claimed_rows[0]
        if leader["state"] == "claimed" and turn_id:
            await self._start_persisted_turn(turn_id)
            return self._committed_delivery_result(
                delivery_id,
                attempted_turn_id=turn_id,
            )
        elif leader["state"] == "steering" and turn_id and attempt_id and native_id:
            return await self._dispatch_steer_batch(
                steer_backend,
                claimed_rows,
                logical_turn_id=turn_id,
                native_turn_id=native_id,
                attempt_id=attempt_id,
                context=context,
            )
        return DeliveryResult(delivery_id, None, str(leader["state"]), turn_id)

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
        should_drain = False
        materialized = False
        saved: dict[str, Any] | None = None
        accepted_run_ids: list[str] = []
        terminal_target: dict[str, Any] | None = None
        session_id = ""
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
                session_id = str(delivery["session_id"])
                attempt_id = str(delivery.get("current_attempt_id") or "")
                attempt_rows = (
                    delivery_store.attempt_deliveries(conn, attempt_id)
                    if attempt_id
                    else [delivery]
                )
                if outcome_value == SteerOutcome.ACCEPTED.value:
                    accepted_run_ids = list(
                        dict.fromkeys(
                            run_id
                            for row in attempt_rows
                            for run_id in delivery_store.agent_run_ids_for_delivery(conn, row)
                        )
                    )
                    accepted_rows = delivery_store.materialize_steer_acceptance(
                        conn,
                        leader_delivery_id=delivery_id,
                        expected_attempt_id=attempt_id,
                        turn_id=target_turn_id,
                        evidence={"kind": "steer_receipt", "receipt": body},
                    )
                    if not accepted_rows:
                        return DeliveryResult(
                            delivery_id,
                            None,
                            "reconciling_steer",
                            target_turn_id or None,
                            "receipt_cas_lost",
                        )
                    saved = accepted_rows[0]
                    materialized = True
                    target_turn = delivery_store.get_turn(conn, target_turn_id)
                    if (
                        accepted_run_ids
                        and target_turn is not None
                        and target_turn["state"] == "terminal"
                    ):
                        terminal_target = target_turn
                if outcome_value == SteerOutcome.UNKNOWN.value:
                    unknown_rows = delivery_store.mark_attempt_receipt_batch(
                        conn,
                        leader_delivery_id=delivery_id,
                        outcome="unknown",
                        receipt=body,
                    )
                    saved = unknown_rows[0] if unknown_rows else None
                    return DeliveryResult(
                        delivery_id,
                        None,
                        "reconciling_steer",
                        target_turn_id or None,
                        None if unknown_rows and all(unknown_rows) else "receipt_cas_lost",
                    )
                if not materialized:
                    session_status = conn.execute(
                        select(agent_sessions.c.status).where(
                            agent_sessions.c.id == str(delivery["session_id"])
                        )
                    ).scalar_one_or_none()
                    next_state = "queued" if session_status == "active" else "retired"
                    fallback_rows = [
                        self._record_definitive_delivery_attempt(
                            conn,
                            str(row["id"]),
                            expected_version=int(row["version"]),
                            expected_states=(str(row["state"]),),
                            outcome=outcome_value,
                            next_state=next_state,
                            next_priority="p3",
                            receipt=body,
                        )
                        for row in attempt_rows
                    ]
                    if not fallback_rows or not all(fallback_rows):
                        return DeliveryResult(
                            delivery_id,
                            None,
                            "reconciling_steer",
                            target_turn_id or None,
                            "fallback_cas_lost",
                        )
                    saved = fallback_rows[0]
                    should_drain = saved is not None and saved["state"] == "queued"
        except Exception:
            logger.exception("failed to persist steering receipt for delivery=%s", delivery_id)
            try:
                with self._sqlite_engine().begin() as conn:
                    reserve_write_lock(conn)
                    persisted_outcome = (
                        "accepted"
                        if outcome_value == SteerOutcome.ACCEPTED.value
                        else "unknown"
                    )
                    persisted_rows = delivery_store.mark_attempt_receipt_batch(
                        conn,
                        leader_delivery_id=delivery_id,
                        outcome=persisted_outcome,
                        receipt=(
                            body
                            if persisted_outcome == "accepted"
                            else {"reason": "receipt_persistence_lost"}
                        ),
                    )
                    if not persisted_rows:
                        raise RuntimeError("steer receipt batch is no longer recoverable")
            except Exception:
                logger.exception(
                    "failed to persist steer receipt recovery fence for delivery=%s",
                    delivery_id,
                )
            return DeliveryResult(
                delivery_id,
                None,
                "reconciling_steer",
                reason="receipt_persistence_lost",
            )

        if materialized:
            self._attach_accepted_agent_runs(
                session_id=session_id,
                turn_id=target_turn_id,
                run_ids=accepted_run_ids,
                context=context,
            )
            if terminal_target is not None:
                self._settle_agent_run_ids_from_terminal_turn(
                    accepted_run_ids,
                    terminal_target,
                )
            self._publish_materialized_delivery(delivery_id)
            return DeliveryResult(
                delivery_id,
                str((saved or {}).get("message_id") or delivery_id),
                "accepted",
                target_turn_id or None,
                admission="steered",
            )
        if should_drain:
            await self.drain_delivery_queue(session_id)
            return self._committed_delivery_result(
                delivery_id,
            )
        return DeliveryResult(
            delivery_id,
            None,
            str((saved or {}).get("state") or "reconciling_steer"),
            None,
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
        should_cancel_prewrite = False
        joined = False
        with self._runtime_start_owner(
            request.session_id,
            backend,
        ) as start_owner, run_update_event_transaction(self._sqlite_engine()) as conn:
            reserve_write_lock(conn)
            session_status = conn.execute(
                select(agent_sessions.c.status).where(
                    agent_sessions.c.id == request.session_id
                )
            ).scalar_one_or_none()
            current = delivery_store.active_turn(conn, request.session_id)
            expected_exclusive_run_id = str(
                request.expected_exclusive_agent_run_id or ""
            ).strip()
            if request.content is not None and session_status != "active":
                existing = (
                    delivery_store.get_delivery(conn, request.delivery_id)
                    if request.delivery_id
                    else None
                )
                if existing is not None and existing["state"] == "reserved":
                    self._retire_delivery_not_written(
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
                    if expected_exclusive_run_id:
                        cancellation = apply_live_agent_run_cancellation_in_connection(
                            conn,
                            expected_exclusive_run_id,
                            session_id=request.session_id,
                            detach=True,
                        )
                        return DeliveryResult(
                            None,
                            None,
                            (
                                "run_detached"
                                if cancellation == "run_detached"
                                else "settled"
                            ),
                            reason=cancellation,
                        )
                    return DeliveryResult(None, None, "settled", reason="not_active")
                delivery = self._insert_delivery(
                    conn,
                    request,
                    priority="p0",
                    state="reserved",
                )
                delivery_id = str(delivery["id"])
                successor_id = delivery_store.new_turn_id()
                claimed = self._claim_start_batch(
                    conn,
                    owner=start_owner,
                    turn_id=successor_id,
                    session_id=request.session_id,
                    backend=backend,
                    deliveries=[delivery],
                    dispatch_text=str(delivery.get("dispatch_text") or ""),
                )
                if claimed is None:
                    successor_id = None
            else:
                interrupt_target_id = current_id
                delivery = None
                expected_turn_id = str(request.expected_turn_id or "").strip()
                if (
                    request.content is None
                    and expected_turn_id
                    and current_id != expected_turn_id
                    and not expected_exclusive_run_id
                ):
                    return DeliveryResult(
                        None,
                        None,
                        "settled",
                        current_id,
                        "target_turn_changed",
                    )
                if request.content is None and expected_exclusive_run_id:
                    exclusive, reason = delivery_store.agent_run_exclusively_owns_turn(
                        conn,
                        run_id=expected_exclusive_run_id,
                        turn_id=str(current_id or ""),
                    )
                    replacement_terminalized = False
                    if not exclusive:
                        replacement_terminalized = (
                            self._terminalize_detached_run_replacement(
                                conn,
                                run_id=expected_exclusive_run_id,
                                session_id=request.session_id,
                                current=current,
                            )
                        )
                    cancellation = apply_live_agent_run_cancellation_in_connection(
                        conn,
                        expected_exclusive_run_id,
                        session_id=request.session_id,
                        detach=not exclusive,
                    )
                    if replacement_terminalized and cancellation != "run_detached":
                        raise RuntimeError(
                            "replacement Run terminalized without cancellation ownership"
                        )
                    if not exclusive:
                        return DeliveryResult(
                            None,
                            None,
                            (
                                "run_detached"
                                if cancellation == "run_detached"
                                else "settled"
                            ),
                            current_id,
                            reason if cancellation == "run_detached" else cancellation,
                        )
                    if cancellation != "cancel_requested":
                        return DeliveryResult(
                            None,
                            None,
                            "settled",
                            current_id,
                            cancellation,
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
                        state="reserved",
                    )
                    delivery_id = str(delivery["id"])
                    if delivery["state"] != "reserved":
                        return DeliveryResult(
                            delivery_id,
                            str(delivery.get("message_id") or "") or None,
                            str(delivery["state"]),
                            str(delivery.get("current_target_turn_id") or "") or None,
                        )
                if control_in_progress:
                    joined = True
                    if request.content is None and current.get("control_mode") == "replace":
                        successor_turn_id = str(
                            current.get("control_successor_turn_id") or ""
                        )
                        successor_delivery_id = str(
                            current.get("control_successor_delivery_id") or ""
                        )
                        successor = delivery_store.get_turn(conn, successor_turn_id)
                        successor_delivery = delivery_store.get_delivery(
                            conn,
                            successor_delivery_id,
                        )
                        if (
                            successor is None
                            or successor["state"] != "waiting"
                            or successor_delivery is None
                            or successor_delivery["state"] != "interrupt_waiting"
                        ):
                            return DeliveryResult(
                                None,
                                None,
                                "refused",
                                current_id,
                                "replacement_supersede_lost",
                            )
                        terminalized = self._write_terminal_snapshot(
                            conn,
                            successor_turn_id,
                            outcome="not_written",
                            settled_by="explicit_stop",
                            evidence_kind="replacement_superseded_by_stop",
                        )
                        retired = self._record_definitive_delivery_attempt(
                            conn,
                            successor_delivery_id,
                            expected_version=int(successor_delivery["version"]),
                            expected_states=("interrupt_waiting",),
                            outcome="not_written",
                            next_state="retired",
                            receipt={"kind": "replacement_superseded_by_stop"},
                        )
                        if not terminalized.get("changed") or retired is None:
                            raise RuntimeError("replacement supersession lost")
                        superseded = delivery_store.cas_turn(
                            conn,
                            current_id,
                            expected_version=int(current["version"]),
                            expected_states=(str(current["state"]),),
                            values={
                                "control_mode": "stop_only",
                                "control_successor_delivery_id": None,
                                "control_successor_turn_id": None,
                            },
                        )
                        if superseded is None:
                            raise RuntimeError("replacement control supersession lost")
                    if delivery is not None:
                        # Only the control-slot winner may replace the active Turn.
                        # A content loser remains one FIFO submission and never calls Stop.
                        queued = delivery_store.cas_delivery(
                            conn,
                            delivery_id,
                            expected_version=int(delivery["version"]),
                            expected_states=("reserved",),
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
                        claimed_delivery = delivery_store.cas_delivery(
                            conn,
                            delivery_id,
                            expected_version=int(delivery["version"]),
                            expected_states=("reserved",),
                            values={
                                "priority": "p0",
                                "state": "interrupt_waiting",
                                "turn_id": successor_id,
                                "turn_role": "initial",
                                "turn_position": 0,
                            },
                            history_event={
                                "kind": "interrupt_join",
                                "target_turn_id": current_id,
                                "successor_turn_id": successor_id,
                                "outcome": "control_slot_claimed",
                            },
                        )
                        if claimed_delivery is None:
                            raise RuntimeError("P0 successor reservation claim lost")
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
                            "control_receipt_json": "{}",
                            "control_successor_delivery_id": delivery_id,
                            "control_successor_turn_id": successor_id,
                        },
                    )
                    if claimed_control is None:
                        raise RuntimeError("P0 control-slot claim lost")
                    should_interrupt = current["state"] == "active"
                    projected = self.in_flight.get(request.session_id)
                    should_cancel_prewrite = bool(
                        current["state"] == "starting"
                        and projected is not None
                        and projected.logical_turn_id == current_id
                        and not projected.task.done()
                        and backend_dispatch_attempted(projected.context) is False
                    )
                    if should_cancel_prewrite:
                        claimed_control = delivery_store.cas_turn(
                            conn,
                            current_id,
                            expected_version=int(claimed_control["version"]),
                            expected_states=("starting",),
                            values={"control_state": "interrupting"},
                        )
                        if claimed_control is None:
                            raise RuntimeError("pre-write P0 control claim lost")

        if current is None and successor_id:
            await self._start_persisted_turn(successor_id, context=context)
            return self._committed_delivery_result(
                str(delivery_id),
                attempted_turn_id=successor_id,
            )
        if current is None and delivery_id:
            return self._committed_delivery_result(delivery_id)
        if joined:
            return DeliveryResult(
                delivery_id,
                None,
                "queued" if delivery_id else "interrupt_waiting",
                interrupt_target_id,
                "joined_existing_interrupt",
            )
        if should_cancel_prewrite:
            canceled = await self._cancel_prewrite_durable_turn(
                request.session_id,
                interrupt_target_id,
            )
            if delivery_id is not None:
                return self._committed_delivery_result(
                    delivery_id,
                    attempted_turn_id=successor_id,
                    reason=canceled.get("reason"),
                )
            return DeliveryResult(
                delivery_id,
                None,
                str(canceled.get("state") or "reconciling"),
                interrupt_target_id,
                canceled.get("reason"),
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

    async def _cancel_prewrite_durable_turn(
        self,
        session_id: str,
        logical_turn_id: str | None,
    ) -> dict[str, Any]:
        """Cancel a live Turn that has definitive evidence of no native write."""

        projected = self.in_flight.get(session_id)
        if (
            projected is None
            or not logical_turn_id
            or projected.logical_turn_id != logical_turn_id
            or projected.task.done()
            or backend_dispatch_attempted(projected.context) is not False
        ):
            return {"state": "reconciling", "reason": "prewrite_owner_changed"}

        projected.cancel_settled_by = SETTLED_BY_STOPPED
        mark_prewrite_user_stop(projected.context)
        projected.task.cancel()
        await asyncio.gather(projected.task, return_exceptions=True)

        with self._sqlite_engine().connect() as conn:
            turn = delivery_store.get_turn(conn, logical_turn_id)
            initial_delivery_ids = {
                str(row["id"])
                for row in delivery_store.initial_deliveries_for_turn(
                    conn,
                    logical_turn_id,
                )
                if row["state"] == "claimed"
            }
        if turn is None:
            return {"state": "reconciling", "reason": "turn_missing"}
        if turn["state"] == "terminal":
            return {
                "state": "settled",
                "reason": (
                    "prewrite_canceled"
                    if turn.get("settled_by") == SETTLED_BY_STOPPED
                    else "already_terminal"
                ),
            }

        terminal = self._terminalize_durable_turn(
            logical_turn_id,
            "not_written",
            settled_by=SETTLED_BY_STOPPED,
            evidence_kind="user_stop_before_native_write",
            evidence={"reason": "prewrite_canceled"},
            retire_unwritten_delivery_ids=initial_delivery_ids,
            retire_unwritten_attempt_outcome="canceled",
        )
        if not terminal.get("changed"):
            return {"state": "reconciling", "reason": "prewrite_terminal_cas_lost"}

        successor_turn_id = str(terminal.get("successor_turn_id") or "")
        if successor_turn_id:
            await self._start_persisted_turn(successor_turn_id)
            return {"state": "claimed", "reason": "prewrite_canceled"}
        return {"state": "settled", "reason": "prewrite_canceled"}

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
            runtime_turn.cancel_settled_by = SETTLED_BY_STOPPED
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
                        values={
                            "state": "queued",
                            "priority": "p3",
                            "turn_id": None,
                            "turn_role": None,
                            "turn_position": None,
                        },
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
                    **(
                        {
                            "control_mode": None,
                            "control_successor_delivery_id": None,
                            "control_successor_turn_id": None,
                        }
                        if definitive and not terminal_proven
                        else {}
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

                bus.publish(
                    "turn.end",
                    _turn_event_payload(session_id, logical_turn_id),
                )
            successor_turn_id = await self._resume_linked_control_successor(
                session_id,
                str(logical_turn_id),
            )
            return {
                "state": "claimed" if successor_turn_id else "settled",
                "reason": "not_active",
            }
        return receipt_result

    async def _start_persisted_turn(
        self,
        turn_id: str,
        *,
        context: Optional["MessageContext"] = None,
        expected_start_attempt_id: str | None = None,
    ) -> bool:
        with self._sqlite_engine().begin() as conn:
            reserve_write_lock(conn)
            turn = delivery_store.get_turn(conn, turn_id)
            if turn is None or turn["state"] != "starting":
                return False
            if (
                expected_start_attempt_id is not None
                and str(turn.get("start_attempt_id") or "")
                != expected_start_attempt_id
            ):
                return False
            backend = str(turn.get("backend") or "").strip()
            if backend in self._draining_backends:
                self._deferred_restart_sessions.setdefault(backend, set()).add(
                    str(turn["session_id"])
                )
                return False
            deliveries = delivery_store.initial_deliveries_for_turn(conn, turn_id)
            delivery = deliveries[0] if deliveries else None
            if (
                delivery is None
                or any(row["state"] != "claimed" for row in deliveries)
                or not turn.get("start_attempt_id")
                or not turn.get("dispatch_sha256")
            ):
                logger.error("durable Turn has no exact start-attempt owner: %s", turn_id)
                return False
            attempt_id = str(turn["start_attempt_id"])
            # The row stays ``starting`` until native acceptance, so the live
            # projection is the launch fence for concurrent resume callers.
            projected = self.in_flight.get(str(turn["session_id"]))
            if projected is not None and not projected.task.done():
                return projected.logical_turn_id == turn_id
        try:
            resolved = (
                self._delivery_context(str(turn["session_id"]))
                if self._build_context is not None
                else context
            )
            if resolved is None:
                raise RuntimeError("durable native start has no Session routing context")
        except Exception:
            logger.exception("durable native start failed before dispatch for Turn=%s", turn_id)
            self._terminalize_durable_turn(
                turn_id,
                "not_written",
                settled_by="pre_write_failure",
                evidence_kind="context_build_failed",
            )
            return False
        lifecycle_anchor = self._lifecycle_anchor_for_delivery(
            delivery,
            str(turn["session_id"]),
        )
        lifecycle_snapshot = self.snapshot_session_lifecycle(lifecycle_anchor)

        archived_before_dispatch = False
        run_terminal_before_dispatch = False
        invalid_delivery_ids: set[str] = set()
        remote_authorization_denials: dict[str, str] = {}
        with self._sqlite_engine().begin() as conn:
            reserve_write_lock(conn)
            latest = delivery_store.get_turn(conn, turn_id)
            session_status = conn.execute(
                select(agent_sessions.c.status).where(
                    agent_sessions.c.id == str(turn["session_id"])
                )
            ).scalar_one_or_none()
            fresh_deliveries = delivery_store.initial_deliveries_for_turn(conn, turn_id)
            fresh_delivery = fresh_deliveries[0] if fresh_deliveries else None
            if (
                latest is None
                or latest["state"] != "starting"
                or latest.get("start_attempt_id") != attempt_id
                or (
                    expected_start_attempt_id is not None
                    and str(latest.get("start_attempt_id") or "")
                    != expected_start_attempt_id
                )
                or fresh_delivery is None
                or latest.get("initial_delivery_id") != fresh_delivery.get("id")
                or any(row["state"] != "claimed" for row in fresh_deliveries)
            ):
                return False
            turn = latest
            deliveries = fresh_deliveries
            delivery = fresh_delivery
            archived_before_dispatch = session_status != "active"
            invalid_delivery_ids = {
                str(row["id"])
                for row in deliveries
                if not self._has_resolvable_delivery_input(conn, row)
            }
            remote_authorization_denials = {
                str(row["id"]): reason
                for row in deliveries
                if (
                    reason := self._remote_delivery_execution_denial(conn, row)
                )
                is not None
            }
            run_ids = list(
                dict.fromkeys(
                    run_id
                    for row in deliveries
                    for run_id in delivery_store.agent_run_ids_for_delivery(conn, row)
                )
            )
            if not archived_before_dispatch and run_ids:
                run_rows = {
                    str(row["id"]): row
                    for row in conn.execute(
                        select(
                            agent_runs.c.id,
                            agent_runs.c.status,
                            agent_runs.c.cancel_requested,
                            agent_runs.c.metadata_json,
                        ).where(agent_runs.c.id.in_(run_ids))
                    ).mappings()
                }
                for run_id in run_ids:
                    run_row = run_rows.get(run_id)
                    if run_row is None:
                        continue
                    run_status = normalize_run_status(run_row["status"])
                    if bool(run_row["cancel_requested"]) or run_status not in {
                        "queued",
                        "running",
                    }:
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
            terminal = self._terminalize_durable_turn(
                turn_id,
                "not_written",
                settled_by="agent_run_terminal",
                evidence_kind="agent_run_terminal_before_native_dispatch",
            )
            if terminal.get("changed"):
                await self._resume_post_terminal(str(turn["session_id"]))
            return False
        if remote_authorization_denials:
            logger.warning(
                "durable Turn=%s failed remote authorization before Agent dispatch: %s",
                turn_id,
                remote_authorization_denials,
            )
            terminal = self._terminalize_durable_turn(
                turn_id,
                "not_written",
                settled_by="remote_authorization_denied",
                evidence_kind="remote_authorization_before_native_dispatch",
                evidence={"denials": remote_authorization_denials},
                retire_unwritten_delivery_ids={
                    str(row["id"])
                    for row in deliveries
                },
            )
            if terminal.get("changed"):
                self._publish_queue_update(str(turn["session_id"]))
                await self._resume_post_terminal(str(turn["session_id"]))
            return False
        if invalid_delivery_ids:
            logger.error(
                "durable Turn=%s lost its resolvable input before native dispatch",
                turn_id,
            )
            terminal = self._terminalize_durable_turn(
                turn_id,
                "not_written",
                settled_by="invalid_input",
                evidence_kind="invalid_input_before_native_dispatch",
                retire_unwritten_delivery_ids=invalid_delivery_ids,
            )
            if terminal.get("changed"):
                self._publish_queue_update(str(turn["session_id"]))
                await self._resume_post_terminal(str(turn["session_id"]))
            return False
        try:
            delivery_payload = self._hydrate_delivery_batch_context(resolved, deliveries)
            resolved.platform_specific["turn_token"] = turn_id
            resolved.platform_specific["delivery_start_attempt_id"] = attempt_id
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
                    resolved.platform_specific.update(
                        {
                            key: value
                            for key, value in preserved.items()
                            if key not in _EXECUTION_ROUTING_KEYS
                        }
                    )
            else:
                native_message_id = str(
                    delivery_payload.get("native_message_id") or ""
                ).strip()
                resolved.message_id = (
                    native_message_id
                    if resolved.platform != "avibe" and native_message_id
                    else str(delivery["id"])
                )
            text = str(turn.get("dispatch_text") or "")
            if source == SOURCE_HUMAN:
                stored = "\n".join(str(row.get("dispatch_text") or "") for row in deliveries if row.get("dispatch_text"))
                if stored and text.endswith(stored):
                    text = text[: -len(stored)] + _segment_dispatch_text(deliveries)
            if source == SOURCE_SCHEDULED:
                # Keep the raw prompt until MessageHandler parses an explicit
                # ``subagent: prompt`` prefix. The handler prepares structured
                # metadata; only the native write renders it as text.
                self._restore_scheduled_dispatch_context(resolved, delivery)
        except Exception:
            logger.exception(
                "durable native start failed during pre-dispatch preparation for Turn=%s",
                turn_id,
            )
            self._terminalize_durable_turn(
                turn_id,
                "not_written",
                settled_by="pre_write_failure",
                evidence_kind="dispatch_preparation_failed",
            )
            return False
        try:
            await self._run(
                str(turn["session_id"]),
                resolved,
                text,
                source=source,
                logical_turn_id=turn_id,
                delivery_id=str((delivery or {}).get("id") or "") or None,
                durable_preallocated=True,
                lifecycle_snapshot=(
                    lifecycle_snapshot if source == SOURCE_HUMAN else None
                ),
            )
            return True
        except Exception:
            logger.exception("durable native start became ambiguous for Turn=%s", turn_id)
            with self._sqlite_engine().begin() as conn:
                reserve_write_lock(conn)
                latest = delivery_store.get_turn(conn, turn_id)
                if (
                    latest is not None
                    and latest["state"] == "starting"
                    and latest.get("start_attempt_id") == attempt_id
                ):
                    delivery_store.mark_start_unknown(
                        conn,
                        expected_version=int(latest["version"]),
                        turn_id=turn_id,
                        receipt={"reason": "dispatch_may_have_written"},
                    )
            return False

    def _delivery_runtime_owner_factory(
        self,
        session_id: str,
        context: Optional["MessageContext"],
    ) -> Callable[[Connection, list[dict[str, Any]]], ContextManager[_RuntimeStartOwner]]:
        """Create a claim callback that binds the exact selected FIFO segment."""

        @contextmanager
        def prepare(
            conn: Connection,
            deliveries: list[dict[str, Any]],
        ) -> Iterator[_RuntimeStartOwner]:
            resolved = context
            if resolved is None:
                durable_backend = str(
                    conn.execute(
                        select(agent_sessions.c.agent_backend).where(agent_sessions.c.id == session_id)
                    ).scalar_one_or_none()
                    or ""
                ).strip()
                if durable_backend:
                    with self._runtime_start_owner_in_transaction(
                        conn,
                        session_id,
                        durable_backend,
                    ) as owner:
                        yield owner
                    return
                try:
                    resolved = self._delivery_context(session_id)
                except Exception:
                    logger.exception(
                        "durable queue drain could not rebuild Session context: session=%s",
                        session_id,
                    )
                    yield _RuntimeStartOwner(
                        session_id=session_id,
                        backend="",
                        session_anchor="",
                        workdir=None,
                        admitted=False,
                    )
                    return
            if deliveries:
                self._apply_delivery_binding_provenance(resolved, deliveries[0])
            backend, _ = self._delivery_backend_in_transaction(
                conn,
                session_id,
                resolved,
            )
            with self._runtime_start_owner_in_transaction(
                conn,
                session_id,
                backend,
            ) as owner:
                yield owner

        return prepare

    async def drain_delivery_queue(
        self,
        session_id: str,
        *,
        expected_head_id: str | None = None,
        expected_head_version: int | None = None,
    ) -> bool:
        turn_id: str | None = None
        with self._sqlite_engine().begin() as conn:
            reserve_write_lock(conn)
            turn_id = self._claim_fifo_batch_in_transaction(
                conn,
                owner=None,
                session_id=session_id,
                backend="",
                expected_head_id=expected_head_id,
                expected_head_version=expected_head_version,
                owner_factory=self._delivery_runtime_owner_factory(session_id, None),
            )
            if turn_id is None:
                return False
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
        expected_start_attempt_id: str | None = None,
        replay_unknown_start: bool = False,
        abandon_unaccepted_start: bool = False,
        retire_unwritten_delivery_ids: set[str] | None = None,
        retire_unwritten_attempt_outcome: str = "invalid_input",
        resume_successors: bool = True,
    ) -> dict[str, Any]:
        if not self._durable_schema_available():
            return {
                "changed": False,
                "successor_turn_id": None,
                "delivery_id": None,
                "defer_queue_resume": False,
            }
        result: dict[str, Any]
        materialized_id: str | None = None
        terminal_run_ids: list[str] = []
        terminal_turn_snapshot: dict[str, Any] | None = None
        projected_status: str | None = None
        status_changed = False
        replayed_unknown_start = False
        unknown_start_exhausted = False
        unknown_start_run_ids: list[str] = []
        forced_retire_ids = retire_unwritten_delivery_ids or set()
        start_deferred = False
        linked_activation_deferred = False
        with self._sqlite_engine().connect() as conn:
            observed_turn = delivery_store.get_turn(conn, turn_id)
        owner_session_id = str((observed_turn or {}).get("session_id") or "")
        owner_backend = str((observed_turn or {}).get("backend") or "")
        with self._runtime_start_owner(owner_session_id, owner_backend) as start_owner, self._sqlite_engine().begin() as conn:
            reserve_write_lock(conn)
            turn = delivery_store.get_turn(conn, turn_id)
            if (
                turn is None
                or turn["state"] == "terminal"
                or (
                    expected_start_attempt_id is not None
                    and turn.get("start_attempt_id") != expected_start_attempt_id
                )
            ):
                result = {
                    "changed": False,
                    "successor_turn_id": None,
                    "delivery_id": None,
                    "defer_queue_resume": False,
                }
            else:
                if outcome not in {"completed", "failed", "canceled", "not_written"}:
                    raise ValueError(f"invalid semantic Turn outcome: {outcome}")
                session_id = str(turn["session_id"])
                session_status = conn.execute(
                    select(agent_sessions.c.status).where(agent_sessions.c.id == session_id)
                ).scalar_one_or_none()
                initial_batch = delivery_store.initial_deliveries_for_turn(conn, turn_id)
                unknown_start_reconciliation = bool(
                    replay_unknown_start
                    and outcome == "failed"
                    and initial_batch
                    and all(row["state"] == "claimed" for row in initial_batch)
                    and turn.get("start_receipt_outcome") == "unknown"
                )
                abandoned_start = bool(
                    abandon_unaccepted_start
                    and outcome == "failed"
                    and initial_batch
                    and all(row["state"] == "claimed" for row in initial_batch)
                    and turn.get("start_receipt_outcome") != "accepted"
                )
                if (
                    initial_batch
                    and all(row["state"] == "claimed" for row in initial_batch)
                    and outcome != "not_written"
                    and turn.get("start_receipt_outcome") != "accepted"
                    and not unknown_start_reconciliation
                    and not abandoned_start
                ):
                    return {
                        "changed": False,
                        "successor_turn_id": None,
                        "delivery_id": None,
                        "defer_queue_resume": True,
                        "reason": "start_acceptance_unproven",
                    }
                if initial_batch and all(row["state"] == "claimed" for row in initial_batch):
                    if abandoned_start:
                        for initial in initial_batch:
                            retired = self._record_definitive_delivery_attempt(
                                conn,
                                str(initial["id"]),
                                expected_version=int(initial["version"]),
                                expected_states=("claimed",),
                                outcome="backend_refresh_failed",
                                next_state="retired",
                                receipt={"kind": evidence_kind, **(evidence or {})},
                            )
                            if retired is None:
                                raise RuntimeError(
                                    "terminal settlement lost an unresolved start Delivery"
                                )
                    elif unknown_start_reconciliation:
                        run_rows = []
                        run_ids = list(
                            dict.fromkeys(
                                run_id
                                for initial in initial_batch
                                for run_id in delivery_store.agent_run_ids_for_delivery(
                                    conn, initial
                                )
                            )
                        )
                        if run_ids:
                            run_rows = list(
                                conn.execute(
                                    select(
                                        agent_runs.c.status,
                                        agent_runs.c.cancel_requested,
                                    ).where(agent_runs.c.id.in_(run_ids))
                                ).mappings()
                            )
                        run_can_replay = len(run_rows) == len(run_ids) and all(
                            not bool(row["cancel_requested"])
                            and normalize_run_status(row["status"]) in {"queued", "running"}
                            for row in run_rows
                        )
                        replayed_unknown_start = bool(
                            session_status == "active"
                            and not turn.get("control_mode")
                            and run_can_replay
                            and _start_replay_count(initial_batch)
                            < _MAX_AUTOMATIC_UNKNOWN_START_REPLAYS
                        )
                        unknown_start_exhausted = not replayed_unknown_start
                        unknown_start_run_ids = run_ids
                        next_state = "queued" if replayed_unknown_start else "retired"
                        attempt_outcome = (
                            "restart_replayed"
                            if replayed_unknown_start
                            else "restart_retry_exhausted"
                        )
                        for initial in initial_batch:
                            reconciled = self._record_definitive_delivery_attempt(
                                conn,
                                str(initial["id"]),
                                expected_version=int(initial["version"]),
                                expected_states=("claimed",),
                                outcome=attempt_outcome,
                                next_state=next_state,
                                next_priority=str(initial["priority"]),
                                receipt={"kind": evidence_kind, **(evidence or {})},
                            )
                            if reconciled is None:
                                raise RuntimeError(
                                    "unknown start recovery lost a Delivery batch CAS"
                                )
                    elif outcome == "not_written":
                        for initial in initial_batch:
                            retire_unwritten = str(initial["id"]) in forced_retire_ids
                            owned_run_terminal = False
                            run_ids = (
                                []
                                if retire_unwritten
                                else delivery_store.agent_run_ids_for_delivery(conn, initial)
                            )
                            if run_ids:
                                run_rows = list(
                                    conn.execute(
                                        select(
                                            agent_runs.c.status,
                                            agent_runs.c.cancel_requested,
                                        ).where(
                                            agent_runs.c.id.in_(run_ids)
                                        )
                                    ).mappings()
                                )
                                owned_run_terminal = bool(
                                    run_rows
                                    and all(
                                        bool(row["cancel_requested"])
                                        or normalize_run_status(row["status"])
                                        not in {"queued", "running"}
                                        for row in run_rows
                                    )
                                )
                            next_state = "queued"
                            if retire_unwritten or session_status != "active" or owned_run_terminal:
                                next_state = "retired"
                            definitive = self._record_definitive_delivery_attempt(
                                conn,
                                str(initial["id"]),
                                expected_version=int(initial["version"]),
                                expected_states=("claimed",),
                                outcome=(
                                    retire_unwritten_attempt_outcome
                                    if retire_unwritten
                                    else "not_written"
                                ),
                                next_state=next_state,
                                next_priority="p3",
                                receipt={"kind": evidence_kind, **(evidence or {})},
                            )
                            if definitive is None:
                                raise RuntimeError(
                                    "terminal no-write evidence lost a Delivery batch CAS"
                                )
                    else:
                        accepted = delivery_store.materialize_start_acceptance(
                            conn,
                            turn_id=turn_id,
                            evidence={"kind": "terminal_proof", "outcome": outcome},
                        )
                        if not accepted:
                            raise RuntimeError(
                                "terminal evidence could not materialize initial Delivery batch"
                            )
                        materialized_id = str(accepted[0].get("message_id") or accepted[0]["id"])
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
                        "defer_queue_resume": False,
                    }
                else:
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
                        fallback = self._record_definitive_delivery_attempt(
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
                                retired = self._record_definitive_delivery_attempt(
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
                            elif resume_successors:
                                terminal_predecessor = delivery_store.get_turn(
                                    conn, turn_id
                                )
                                started = self._activate_linked_waiting_successor(
                                    conn,
                                    owner=start_owner,
                                    predecessor=terminal_predecessor,
                                )
                                if started is None:
                                    start_deferred = True
                                    linked_activation_deferred = True
                                else:
                                    claimed_successor = successor_turn_id
                            else:
                                deferred = self._write_terminal_snapshot(
                                    conn,
                                    successor_turn_id,
                                    outcome="not_written",
                                    settled_by=settled_by,
                                    evidence_kind="service_shutdown_deferred_successor",
                                    evidence={"reason": "service_shutdown"},
                                )
                                queued = delivery_store.cas_delivery(
                                    conn,
                                    successor_delivery_id,
                                    expected_version=int(successor_delivery["version"]),
                                    expected_states=("interrupt_waiting",),
                                    values={
                                        "state": "queued",
                                        "priority": "p3",
                                        "turn_id": None,
                                        "turn_role": None,
                                        "turn_position": None,
                                    },
                                    history_event={
                                        "kind": "interrupt_join",
                                        "turn_id": turn_id,
                                        "outcome": "service_shutdown_deferred",
                                    },
                                )
                                if not deferred.get("changed") or queued is None:
                                    raise RuntimeError(
                                        "service shutdown lost its deferred successor"
                                    )
                    if (
                        claimed_successor is None
                        and resume_successors
                        and replayed_unknown_start
                        and session_status == "active"
                    ):
                        retry_rows = [
                            delivery_store.get_delivery(conn, str(initial["id"]))
                            for initial in initial_batch
                        ]
                        if any(row is None or row["state"] != "queued" for row in retry_rows):
                            raise RuntimeError(
                                "unknown start recovery lost its replayable Delivery batch"
                            )
                        claimed_successor = delivery_store.new_turn_id()
                        claimed = self._claim_start_batch(
                            conn,
                            owner=start_owner,
                            turn_id=claimed_successor,
                            session_id=session_id,
                            backend=str(turn["backend"]),
                            deliveries=[row for row in retry_rows if row is not None],
                            dispatch_text=(
                                _UNKNOWN_START_REPLAY_INSTRUCTION
                                + str(turn.get("dispatch_text") or "")
                            ),
                        )
                        if claimed is None:
                            claimed_successor = None
                            start_deferred = True
                    if (
                        claimed_successor is None
                        and resume_successors
                        and session_status == "active"
                        and (outcome != "not_written" or bool(forced_retire_ids))
                    ):
                        claimed_successor = self._claim_fifo_batch_in_transaction(
                            conn,
                            owner=start_owner,
                            session_id=session_id,
                            backend=str(turn["backend"]),
                        )
                        if (
                            claimed_successor is None
                            and delivery_store.claimable_fifo_head(conn, session_id)
                            is not None
                        ):
                            start_deferred = True
                    elif (
                        claimed_successor is None
                        and (
                            not resume_successors
                            or (outcome == "not_written" and not forced_retire_ids)
                        )
                        and session_status == "active"
                        and delivery_store.claimable_fifo_head(conn, session_id)
                        is not None
                    ):
                        start_deferred = True
                    latest_turn = delivery_store.get_turn(conn, turn_id)
                    if (
                        not linked_activation_deferred
                        and latest_turn is not None
                        and latest_turn.get("control_state") in {
                        "pending",
                        "interrupting",
                        "waiting_terminal",
                        "reconciling",
                        }
                    ):
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
                        "defer_queue_resume": start_deferred,
                        "unknown_start_exhausted": unknown_start_exhausted,
                    }
                    terminal_run_ids = (
                        unknown_start_run_ids
                        if unknown_start_exhausted
                        else delivery_store.accepted_agent_run_ids_for_turn(
                            conn,
                            turn_id,
                        )
                    )
                    terminal_turn_snapshot = delivery_store.get_turn(conn, turn_id)
                    projected_status = (
                        "running"
                        if claimed_successor
                        else "failed"
                        if outcome == "failed"
                        else "idle"
                    )
                    projected = conn.execute(
                        update(agent_sessions)
                        .where(agent_sessions.c.id == session_id)
                        .where(agent_sessions.c.agent_status != projected_status)
                        .values(agent_status=projected_status)
                    )
                    status_changed = bool(projected.rowcount)
        if result.get("changed"):
            run_settled_by = (
                SETTLED_BY_NO_TERMINAL_RESULT
                if result.get("unknown_start_exhausted")
                else settled_by
            )
            if (
                terminal_turn_snapshot is not None
                and run_settled_by in SETTLEMENTS_WITHOUT_RESULT
            ):
                terminal_turn_snapshot = {
                    **terminal_turn_snapshot,
                    "settled_by": run_settled_by,
                }
                self._settle_agent_run_ids_from_terminal_turn(
                    terminal_run_ids,
                    terminal_turn_snapshot,
                )
            else:
                self._settle_agent_run_ids(terminal_run_ids, run_settled_by)
        if materialized_id:
            self._publish_materialized_delivery(materialized_id)
        if result.get("changed"):
            self._publish_terminal_inbox_update(session_id)
        if status_changed and projected_status is not None:
            from core.inbox_events import bus

            bus.publish(
                "session.status",
                {"session_id": session_id, "agent_status": projected_status},
            )
        return result

    def reconcile_start_attempt_not_written(
        self,
        turn_id: str,
        attempt_id: str,
        *,
        backend: str,
    ) -> bool:
        """Consume exact adapter proof that a persisted start never reached native."""

        if not turn_id or not attempt_id:
            return False
        result = self._terminalize_durable_turn(
            turn_id,
            "not_written",
            settled_by="adapter_start_absent",
            evidence_kind="native_start_attempt_absent",
            evidence={"backend": backend, "attempt_id": attempt_id},
            expected_start_attempt_id=attempt_id,
        )
        return bool(result.get("changed"))

    def settle_start_attempt_invalid_input(
        self,
        turn_id: str,
        attempt_id: str,
        *,
        backend: str,
    ) -> bool:
        """Retire a start batch rejected permanently before any native write."""

        if not turn_id or not attempt_id:
            return False
        with self._sqlite_engine().connect() as conn:
            turn = delivery_store.get_turn(conn, turn_id)
            if turn is None or turn.get("start_attempt_id") != attempt_id:
                return False
            session_id = str(turn["session_id"])
            delivery_ids = {
                str(row["id"])
                for row in delivery_store.initial_deliveries_for_turn(conn, turn_id)
            }
        if not delivery_ids:
            return False
        result = self._terminalize_durable_turn(
            turn_id,
            "not_written",
            settled_by="adapter_start_invalid_input",
            evidence_kind="native_start_invalid_input",
            evidence={"backend": backend, "attempt_id": attempt_id},
            expected_start_attempt_id=attempt_id,
            retire_unwritten_delivery_ids=delivery_ids,
        )
        if result.get("changed"):
            self._publish_queue_update(session_id)
            if not result.get("defer_queue_resume"):
                asyncio.create_task(
                    self._resume_after_native_terminal(session_id, turn_id),
                    name=f"durable-terminal-resume:{session_id}",
                )
        return bool(result.get("changed"))

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
        definitive_prewrite_exit: bool,
        settled_by: str | None,
        terminal_is_error: bool,
        cancel_defers_queue_resume: bool = False,
    ) -> dict[str, Any]:
        """Contain post-native ownership writes so runner cleanup always finishes."""

        try:
            if cancelled:
                interruption = settled_by or SETTLED_BY_NO_TERMINAL_RESULT
                resume_after_cancel = bool(
                    interruption == SETTLED_BY_STOPPED
                    and not cancel_defers_queue_resume
                )
                # Same map as every other Turn-outcome surface. This branch used
                # to hardcode ``stopped``, so a rolling refresh landed ``failed``
                # here while ``release_for_backend_refresh`` -- the very caller
                # that cancelled this runner -- wrote ``canceled`` for the durable
                # Turns it reached directly. One teardown, two outcomes, decided
                # by which writer won the race. ``restarted`` is absent from the
                # map and still falls through to ``failed``: a service shutdown
                # is not a cancellation.
                result = self._terminalize_durable_turn(
                    turn_id,
                    NON_COMPLETING_TURN_SETTLEMENTS.get(interruption, "failed"),
                    settled_by=interruption,
                    evidence_kind=(
                        "service_shutdown"
                        if interruption == SETTLED_BY_RESTARTED
                        else "runner_release"
                    ),
                    evidence=(
                        {"reason": "scheduled_service_shutdown"}
                        if interruption == SETTLED_BY_RESTARTED
                        else None
                    ),
                    resume_successors=resume_after_cancel,
                )
                if not resume_after_cancel:
                    result["defer_queue_resume"] = True
                return result
            if failed:
                # Dispatch may have written before raising. Preserve starting work
                # for exact-evidence recovery instead of replaying it.
                with self._sqlite_engine().begin() as conn:
                    reserve_write_lock(conn)
                    turn = delivery_store.get_turn(conn, turn_id)
                    if turn is not None and turn["state"] == "starting":
                        delivery_store.mark_start_unknown(
                            conn,
                            turn_id,
                            expected_version=int(turn["version"]),
                            receipt={"reason": "runner_dispatch_failure"},
                        )
                return {"defer_queue_resume": True}
            if prewrite_refused:
                return self._settle_durable_prewrite_failure(
                    turn_id,
                    outcome=SETTLED_BY_REFUSED_CONCURRENT_TURN,
                )
            if definitive_prewrite_exit:
                if settled_by == SETTLED_BY_STOPPED:
                    with self._sqlite_engine().connect() as conn:
                        initial_delivery_ids = {
                            str(row["id"])
                            for row in delivery_store.initial_deliveries_for_turn(
                                conn,
                                turn_id,
                            )
                            if row["state"] == "claimed"
                        }
                    return self._terminalize_durable_turn(
                        turn_id,
                        "not_written",
                        settled_by=SETTLED_BY_STOPPED,
                        evidence_kind="user_stop_before_native_write",
                        evidence={"reason": "prewrite_canceled"},
                        retire_unwritten_delivery_ids=initial_delivery_ids,
                        retire_unwritten_attempt_outcome="canceled",
                    )
                return self._settle_durable_prewrite_failure(
                    turn_id,
                    outcome=SETTLED_BY_NO_TERMINAL_RESULT,
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
                )
        except Exception:
            logger.exception(
                "normal turn durable terminal reconciliation deferred for Turn=%s",
                turn_id,
            )
        return {"defer_queue_resume": True}

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
        elif result.get("changed") and not result.get("defer_queue_resume"):
            with self._sqlite_engine().connect() as conn:
                terminal = delivery_store.get_turn(conn, turn_id)
            if terminal is not None:
                await self.drain_delivery_queue(str(terminal["session_id"]))
        return bool(result.get("changed"))

    async def _run_pending_interrupt(
        self,
        session_id: str,
        logical_turn_id: str,
        *,
        expected_state: str | None = None,
        expected_version: int | None = None,
        expected_control_state: str | None = None,
        expected_control_attempt_id: str | None = None,
        expected_native_turn_id: str | None = None,
        expected_successor_delivery_id: str | None = None,
        expected_successor_turn_id: str | None = None,
    ) -> bool:
        with self._sqlite_engine().begin() as conn:
            reserve_write_lock(conn)
            pending = delivery_store.pending_control_for_turn(conn, logical_turn_id)
            if pending is None:
                return False
            if (
                str(pending.get("session_id") or "") != session_id
                or (
                    expected_version is not None
                    and (
                        int(pending.get("version") or 0) != expected_version
                        or str(pending.get("state") or "")
                        != str(expected_state or "")
                        or str(pending.get("control_state") or "")
                        != str(expected_control_state or "")
                        or str(pending.get("control_attempt_id") or "")
                        != str(expected_control_attempt_id or "")
                        or str(
                            pending.get("control_expected_native_turn_id") or ""
                        )
                        != str(expected_native_turn_id or "")
                        or str(pending.get("control_successor_delivery_id") or "")
                        != str(expected_successor_delivery_id or "")
                        or str(pending.get("control_successor_turn_id") or "")
                        != str(expected_successor_turn_id or "")
                    )
                )
            ):
                return False
            claimed = delivery_store.cas_turn(
                conn,
                logical_turn_id,
                expected_version=int(pending["version"]),
                expected_states=("active",),
                values={"control_state": "interrupting"},
            )
        if claimed is not None:
            await self._interrupt_durable_turn(session_id, logical_turn_id)
            return True
        return False

    @staticmethod
    def _delivery_ack_target(delivery: dict[str, Any]) -> Optional[str]:
        """Return the message an admission receipt for this Delivery sits on.

        Usually the sender's own message, but a quick-reply callback is
        dispatched with ``message_id=None`` (to bypass platform event dedup) and
        reacts on its bot echo instead. That echo id only survives in the
        durable admission context.
        """

        admission_context = delivery_store.delivery_admission_context(delivery)
        if isinstance(admission_context, dict):
            target = admission_context.get("processing_indicator_message_id")
            if target:
                return str(target)
        payload = delivery_store.delivery_payload(delivery)
        return str(payload.get("native_message_id") or "").strip() or None

    def _delivery_receipt_context(
        self,
        session_id: str,
        delivery: dict[str, Any],
    ) -> Optional["MessageContext"]:
        """Rebuild just enough routing to react on one Delivery's own message."""

        payload = delivery_store.delivery_payload(delivery)
        platform = str(payload.get("platform") or "")
        native_message_id = str(payload.get("native_message_id") or "").strip()
        target = self._delivery_ack_target(delivery)
        if not platform or platform == "avibe" or not target:
            # Nothing to decorate: only an IM input has a reaction target, and
            # the Workbench composer is P3 (it never reaches a pending steer).
            return None
        context = self._delivery_context(session_id)
        context.platform = platform
        context.message_id = native_message_id or None
        spec = dict(context.platform_specific or {})
        spec["processing_indicator_message_id"] = target
        context.platform_specific = spec
        return context

    async def _report_delivery_receipts(
        self,
        session_id: str,
        deliveries: list[dict[str, Any]],
        *,
        state: str,
        admission: str = "",
    ) -> None:
        """Report the admission outcome of Deliveries settled away from ingress.

        The admission call that returned ``pending_steer`` already told the
        sender its message was queued, but the attempt that resolves it runs
        later (``_run_pending_steers``, recovery) and its result is consumed
        there, not by the ingress caller — so this is the only place that can
        report that the message joined the running turn (✍️), was definitively
        refused (🤷), or is still unconfirmed (🤔).
        """

        indicator = getattr(self.controller, "processing_indicator", None)
        ack = getattr(indicator, "ack_delivery_state", None)
        if not callable(ack):
            return
        for delivery in deliveries or []:
            try:
                context = self._delivery_receipt_context(session_id, delivery)
                if context is None:
                    continue
                await ack(context, state=state, admission=admission)
            except Exception as err:
                logger.debug("Failed to report admission receipt: %s", err)

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
                attempt_id = str(pending.get("current_attempt_id") or "")
                pending_batch = delivery_store.attempt_deliveries(conn, attempt_id)
                if not attempt_id or not pending_batch:
                    return
                claimed_batch = delivery_store.open_steer_attempt_batch(
                    conn,
                    deliveries=pending_batch,
                    turn_id=logical_turn_id,
                    attempt_id=attempt_id,
                    expected_native_turn_id=native_turn_id,
                )
                if not claimed_batch:
                    continue
                backend = str(turn["backend"])
            result = await self._dispatch_steer_batch(
                backend,
                claimed_batch,
                logical_turn_id=logical_turn_id,
                native_turn_id=native_turn_id,
                attempt_id=attempt_id,
                context=context,
            )
            # Every row of the attempt settles together, so the leader's outcome
            # is the batch's outcome: accepted upgrades 👌 to ✍️, a definitive
            # refusal after the Session went inactive retires it to 🤷, and an
            # unconfirmed receipt reports 🤔.
            await self._report_delivery_receipts(
                session_id,
                claimed_batch,
                state=result.state,
                admission=result.admission,
            )

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
                "queue.updated",
                {"session_id": row.get("session_id")},
            )
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

    @staticmethod
    def _publish_queue_update(session_id: str) -> None:
        try:
            from core.inbox_events import bus

            bus.publish("queue.updated", {"session_id": session_id})
        except Exception:
            logger.exception("Delivery queue update publish failed for %s", session_id)

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
                materialized: list[dict[str, Any]] = []
                if bound is not None:
                    materialized = delivery_store.materialize_start_acceptance(
                        conn,
                        turn_id=logical_turn_id,
                        evidence={
                            "kind": "native_start",
                            "runtime_key": runtime_key,
                            "runtime_turn_id": runtime_turn_id,
                            "native_turn_id": native_turn_id,
                        },
                    )
                    if not materialized:
                        raise RuntimeError("native start could not materialize initial Delivery batch")
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
        if materialized:
            self._publish_materialized_delivery(
                str(materialized[0].get("message_id") or materialized[0]["id"])
            )
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
            if callable(get_sink):
                try:
                    sink = get_sink(resolve_turn_sink_key(self.controller, context))
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
            current is None and not bool(result.get("defer_queue_resume"))
        )
        if result.get("changed") and should_resume:
            return asyncio.create_task(
                self._resume_after_native_terminal(session_id, logical_turn_id),
                name=f"durable-terminal-resume:{session_id}",
            )
        return None

    def scan_runtime_delivery_recovery(
        self,
        *,
        limit: int,
        occupied: frozenset[str],
        cursor: str | None = None,
    ) -> tuple[list[RuntimeDeliveryObservation], bool]:
        """Return a bounded keyset page of exact Session recovery owners."""

        page_limit = max(1, int(limit))
        with self._sqlite_engine().connect() as conn:
            predecessor_rows = session_turn_rows.alias("recovery_predecessor")
            live_turn = exists(
                select(session_turn_rows.c.id)
                .where(
                    session_turn_rows.c.session_id == agent_sessions.c.id,
                    session_turn_rows.c.state.in_(delivery_store.TURN_OWNER_STATES),
                )
                .correlate(agent_sessions)
            )
            head_id = (
                select(delivery_rows.c.id)
                .where(
                    delivery_rows.c.session_id == agent_sessions.c.id,
                    delivery_rows.c.state == "queued",
                )
                .order_by(delivery_rows.c.submitted_at, delivery_rows.c.id)
                .limit(1)
                .correlate(agent_sessions)
                .scalar_subquery()
            )
            open_query = (
                select(
                    agent_sessions.c.id.label("session_id"),
                    literal("open_head").label("kind"),
                    delivery_rows.c.id.label("delivery_id"),
                    delivery_rows.c.state.label("delivery_state"),
                    delivery_rows.c.version.label("delivery_version"),
                    delivery_rows.c.current_attempt_id.label("delivery_attempt_id"),
                    delivery_rows.c.current_target_turn_id.label(
                        "delivery_target_turn_id"
                    ),
                    delivery_rows.c.current_expected_native_turn_id.label(
                        "delivery_expected_native_turn_id"
                    ),
                    literal(None).label("turn_id"),
                    literal(None).label("turn_state"),
                    literal(None).label("turn_version"),
                    literal(None).label("start_attempt_id"),
                    literal(None).label("native_turn_id"),
                )
                .join(delivery_rows, delivery_rows.c.id == head_id)
                .where(
                    agent_sessions.c.status == "active",
                    ~live_turn,
                )
                .order_by(
                    agent_sessions.c.id,
                    delivery_rows.c.submitted_at,
                    delivery_rows.c.id,
                )
                .limit(page_limit + 1)
            )
            if cursor:
                open_query = open_query.where(agent_sessions.c.id > cursor)
            if occupied:
                open_query = open_query.where(~agent_sessions.c.id.in_(occupied))
            open_rows = [dict(row) for row in conn.execute(open_query).mappings()]

            turn_candidates = (
                select(
                    session_turn_rows.c.session_id.label("session_id"),
                    literal("turn_owner").label("kind"),
                    delivery_rows.c.id.label("delivery_id"),
                    delivery_rows.c.state.label("delivery_state"),
                    delivery_rows.c.version.label("delivery_version"),
                    delivery_rows.c.current_attempt_id.label("delivery_attempt_id"),
                    delivery_rows.c.current_target_turn_id.label(
                        "delivery_target_turn_id"
                    ),
                    delivery_rows.c.current_expected_native_turn_id.label(
                        "delivery_expected_native_turn_id"
                    ),
                    session_turn_rows.c.id.label("turn_id"),
                    session_turn_rows.c.state.label("turn_state"),
                    session_turn_rows.c.version.label("turn_version"),
                    session_turn_rows.c.start_attempt_id.label("start_attempt_id"),
                    session_turn_rows.c.native_turn_id.label("native_turn_id"),
                    predecessor_rows.c.id.label("predecessor_turn_id"),
                    predecessor_rows.c.state.label("predecessor_state"),
                    predecessor_rows.c.version.label("predecessor_version"),
                    predecessor_rows.c.control_state.label(
                        "predecessor_control_state"
                    ),
                    predecessor_rows.c.control_mode.label(
                        "predecessor_control_mode"
                    ),
                    predecessor_rows.c.control_attempt_id.label(
                        "predecessor_control_attempt_id"
                    ),
                    predecessor_rows.c.control_expected_native_turn_id.label(
                        "predecessor_control_expected_native_turn_id"
                    ),
                    predecessor_rows.c.control_receipt_outcome.label(
                        "predecessor_control_receipt_outcome"
                    ),
                    predecessor_rows.c.control_successor_delivery_id.label(
                        "predecessor_successor_delivery_id"
                    ),
                    predecessor_rows.c.control_successor_turn_id.label(
                        "predecessor_successor_turn_id"
                    ),
                    predecessor_rows.c.terminal_outcome.label(
                        "predecessor_terminal_outcome"
                    ),
                    predecessor_rows.c.settled_by.label("predecessor_settled_by"),
                    predecessor_rows.c.terminal_evidence_kind.label(
                        "predecessor_terminal_evidence_kind"
                    ),
                    predecessor_rows.c.terminal_evidence_json.label(
                        "predecessor_terminal_evidence_json"
                    ),
                    predecessor_rows.c.terminal_at.label("predecessor_terminal_at"),
                    session_turn_rows.c.created_at.label("sort_at"),
                    literal(0).label("sort_kind"),
                )
                .join(
                    delivery_rows,
                    delivery_rows.c.id == session_turn_rows.c.initial_delivery_id,
                )
                .outerjoin(
                    predecessor_rows,
                    and_(
                        predecessor_rows.c.session_id
                        == session_turn_rows.c.session_id,
                        predecessor_rows.c.control_successor_turn_id
                        == session_turn_rows.c.id,
                        predecessor_rows.c.control_successor_delivery_id
                        == delivery_rows.c.id,
                    ),
                )
                .where(
                    or_(
                        session_turn_rows.c.state.in_(("waiting", "starting")),
                        and_(
                            session_turn_rows.c.state == "active",
                            session_turn_rows.c.control_state.in_(
                                (
                                    "pending",
                                    "interrupting",
                                    "waiting_terminal",
                                    "reconciling",
                                )
                            ),
                        ),
                    )
                )
                .order_by(
                    session_turn_rows.c.session_id,
                    session_turn_rows.c.created_at,
                    session_turn_rows.c.id,
                )
                .limit(page_limit + 1)
            )
            if cursor:
                turn_candidates = turn_candidates.where(
                    session_turn_rows.c.session_id > cursor
                )
            if occupied:
                turn_candidates = turn_candidates.where(
                    ~session_turn_rows.c.session_id.in_(occupied)
                )
            fence_candidates = (
                select(
                    delivery_rows.c.session_id.label("session_id"),
                    literal("delivery_fence").label("kind"),
                    delivery_rows.c.id.label("delivery_id"),
                    delivery_rows.c.state.label("delivery_state"),
                    delivery_rows.c.version.label("delivery_version"),
                    delivery_rows.c.current_attempt_id.label("delivery_attempt_id"),
                    delivery_rows.c.current_target_turn_id.label(
                        "delivery_target_turn_id"
                    ),
                    delivery_rows.c.current_expected_native_turn_id.label(
                        "delivery_expected_native_turn_id"
                    ),
                    session_turn_rows.c.id.label("turn_id"),
                    session_turn_rows.c.state.label("turn_state"),
                    session_turn_rows.c.version.label("turn_version"),
                    session_turn_rows.c.start_attempt_id.label("start_attempt_id"),
                    session_turn_rows.c.native_turn_id.label("native_turn_id"),
                    delivery_rows.c.submitted_at.label("sort_at"),
                    literal(1).label("sort_kind"),
                )
                .select_from(
                    delivery_rows.outerjoin(
                        session_turn_rows,
                        session_turn_rows.c.id
                        == delivery_rows.c.current_target_turn_id,
                    )
                )
                .where(delivery_rows.c.state.in_(delivery_store.FENCE_STATES))
                .order_by(
                    delivery_rows.c.session_id,
                    delivery_rows.c.submitted_at,
                    delivery_rows.c.id,
                )
                .limit(page_limit + 1)
            )
            if cursor:
                fence_candidates = fence_candidates.where(
                    delivery_rows.c.session_id > cursor
                )
            if occupied:
                fence_candidates = fence_candidates.where(
                    ~delivery_rows.c.session_id.in_(occupied)
                )
            turn_rows = [
                dict(row) for row in conn.execute(turn_candidates).mappings()
            ]
            fence_rows = [
                dict(row) for row in conn.execute(fence_candidates).mappings()
            ]

        by_session: dict[str, dict[str, Any]] = {}
        for row in turn_rows + fence_rows + open_rows:
            session_id = str(row["session_id"])
            current = by_session.get(session_id)
            if current is None or (
                row.get("turn_state") == "waiting"
                and current.get("turn_state") != "waiting"
            ):
                by_session[session_id] = row
        selected = [
            by_session[session_id]
            for session_id in sorted(by_session)[:page_limit]
        ]
        observations = [
            RuntimeDeliveryObservation(
                session_id=str(row["session_id"]),
                kind=str(row["kind"]),
                delivery_id=str(row.get("delivery_id") or "") or None,
                delivery_state=str(row.get("delivery_state") or "") or None,
                delivery_version=(
                    int(row["delivery_version"])
                    if row.get("delivery_version") is not None
                    else None
                ),
                delivery_attempt_id=(
                    str(row.get("delivery_attempt_id") or "") or None
                ),
                delivery_target_turn_id=(
                    str(row.get("delivery_target_turn_id") or "") or None
                ),
                delivery_expected_native_turn_id=(
                    str(row.get("delivery_expected_native_turn_id") or "") or None
                ),
                turn_id=str(row.get("turn_id") or "") or None,
                turn_state=str(row.get("turn_state") or "") or None,
                turn_version=(
                    int(row["turn_version"])
                    if row.get("turn_version") is not None
                    else None
                ),
                start_attempt_id=str(row.get("start_attempt_id") or "") or None,
                native_turn_id=str(row.get("native_turn_id") or "") or None,
                predecessor_turn_id=(
                    str(row.get("predecessor_turn_id") or "") or None
                ),
                predecessor_state=(
                    str(row.get("predecessor_state") or "") or None
                ),
                predecessor_version=(
                    int(row["predecessor_version"])
                    if row.get("predecessor_version") is not None
                    else None
                ),
                predecessor_control_state=(
                    str(row.get("predecessor_control_state") or "") or None
                ),
                predecessor_control_mode=(
                    str(row.get("predecessor_control_mode") or "") or None
                ),
                predecessor_control_attempt_id=(
                    str(row.get("predecessor_control_attempt_id") or "") or None
                ),
                predecessor_control_expected_native_turn_id=(
                    str(
                        row.get("predecessor_control_expected_native_turn_id")
                        or ""
                    )
                    or None
                ),
                predecessor_control_receipt_outcome=(
                    str(row.get("predecessor_control_receipt_outcome") or "")
                    or None
                ),
                predecessor_successor_delivery_id=(
                    str(row.get("predecessor_successor_delivery_id") or "") or None
                ),
                predecessor_successor_turn_id=(
                    str(row.get("predecessor_successor_turn_id") or "") or None
                ),
                predecessor_terminal_outcome=(
                    str(row.get("predecessor_terminal_outcome") or "") or None
                ),
                predecessor_settled_by=(
                    str(row.get("predecessor_settled_by") or "") or None
                ),
                predecessor_terminal_evidence_kind=(
                    str(row.get("predecessor_terminal_evidence_kind") or "")
                    or None
                ),
                predecessor_terminal_evidence_json=(
                    str(row.get("predecessor_terminal_evidence_json") or "")
                    or None
                ),
                predecessor_terminal_at=(
                    str(row.get("predecessor_terminal_at") or "") or None
                ),
            )
            for row in selected
        ]
        has_more = (
            len(open_rows) > page_limit
            or len(turn_rows) > page_limit
            or len(fence_rows) > page_limit
            or len(by_session) > page_limit
        )
        return observations, has_more

    @staticmethod
    def _terminal_predecessor_matches_observation(
        predecessor: dict[str, Any],
        observation: RuntimeDeliveryObservation,
    ) -> bool:
        return bool(
            str(predecessor.get("id") or "")
            == str(observation.predecessor_turn_id or "")
            and str(predecessor.get("session_id") or "") == observation.session_id
            and str(predecessor.get("state") or "")
            == str(observation.predecessor_state or "")
            and int(predecessor.get("version") or 0)
            == observation.predecessor_version
            and str(predecessor.get("control_state") or "")
            == str(observation.predecessor_control_state or "")
            and str(predecessor.get("control_mode") or "")
            == str(observation.predecessor_control_mode or "")
            and str(predecessor.get("control_attempt_id") or "")
            == str(observation.predecessor_control_attempt_id or "")
            and str(predecessor.get("control_expected_native_turn_id") or "")
            == str(observation.predecessor_control_expected_native_turn_id or "")
            and str(predecessor.get("control_receipt_outcome") or "")
            == str(observation.predecessor_control_receipt_outcome or "")
            and str(predecessor.get("control_successor_delivery_id") or "")
            == str(observation.predecessor_successor_delivery_id or "")
            and str(predecessor.get("control_successor_turn_id") or "")
            == str(observation.predecessor_successor_turn_id or "")
            and str(predecessor.get("terminal_outcome") or "")
            == str(observation.predecessor_terminal_outcome or "")
            and str(predecessor.get("settled_by") or "")
            == str(observation.predecessor_settled_by or "")
            and str(predecessor.get("terminal_evidence_kind") or "")
            == str(observation.predecessor_terminal_evidence_kind or "")
            and str(predecessor.get("terminal_evidence_json") or "")
            == str(observation.predecessor_terminal_evidence_json or "")
            and str(predecessor.get("terminal_at") or "")
            == str(observation.predecessor_terminal_at or "")
        )

    def _runtime_observation_is_current(
        self,
        observation: RuntimeDeliveryObservation,
    ) -> tuple[
        dict[str, Any] | None,
        dict[str, Any] | None,
        dict[str, Any] | None,
    ]:
        with self._sqlite_engine().connect() as conn:
            delivery = (
                delivery_store.get_delivery(conn, observation.delivery_id)
                if observation.delivery_id
                else None
            )
            turn = (
                delivery_store.get_turn(conn, observation.turn_id)
                if observation.turn_id
                else None
            )
            predecessor = (
                delivery_store.get_turn(conn, observation.predecessor_turn_id)
                if observation.predecessor_turn_id
                else None
            )
        if observation.delivery_id and (
            delivery is None
            or str(delivery.get("session_id") or "") != observation.session_id
            or str(delivery.get("state") or "") != observation.delivery_state
            or int(delivery.get("version") or 0) != observation.delivery_version
            or str(delivery.get("current_attempt_id") or "")
            != str(observation.delivery_attempt_id or "")
            or str(delivery.get("current_target_turn_id") or "")
            != str(observation.delivery_target_turn_id or "")
            or str(delivery.get("current_expected_native_turn_id") or "")
            != str(observation.delivery_expected_native_turn_id or "")
        ):
            return None, None, None
        if observation.turn_id and (
            turn is None
            or str(turn.get("session_id") or "") != observation.session_id
            or str(turn.get("state") or "") != observation.turn_state
            or int(turn.get("version") or 0) != observation.turn_version
            or str(turn.get("start_attempt_id") or "")
            != str(observation.start_attempt_id or "")
            or str(turn.get("native_turn_id") or "")
            != str(observation.native_turn_id or "")
        ):
            return None, None, None
        if observation.predecessor_turn_id and (
            predecessor is None
            or not self._terminal_predecessor_matches_observation(
                predecessor,
                observation,
            )
        ):
            return None, None, None
        return delivery, turn, predecessor

    async def recover_runtime_delivery_observation(
        self,
        observation: RuntimeDeliveryObservation,
    ) -> bool:
        """Re-enter only the exact owner seen by a bounded lane scan."""

        delivery, turn, predecessor = self._runtime_observation_is_current(observation)
        if observation.delivery_id and delivery is None:
            return False
        if observation.turn_id and turn is None:
            return False
        if observation.predecessor_turn_id and predecessor is None:
            return False
        if observation.kind == "open_head":
            return await self.drain_delivery_queue(
                observation.session_id,
                expected_head_id=observation.delivery_id,
                expected_head_version=observation.delivery_version,
            )
        if turn is not None and turn["state"] == "starting":
            return await self._start_persisted_turn(
                str(turn["id"]),
                expected_start_attempt_id=str(turn.get("start_attempt_id") or ""),
            )
        if turn is not None and turn["state"] == "waiting":
            if predecessor is None:
                return False
            predecessor_id = str(predecessor["id"])
            if predecessor["state"] == "terminal":
                return bool(
                    await self._resume_linked_control_successor(
                        observation.session_id,
                        predecessor_id,
                        observation=observation,
                    )
                )
            return await self._run_pending_interrupt(
                observation.session_id,
                predecessor_id,
                expected_state=observation.predecessor_state,
                expected_version=observation.predecessor_version,
                expected_control_state=observation.predecessor_control_state,
                expected_control_attempt_id=(
                    observation.predecessor_control_attempt_id
                ),
                expected_native_turn_id=(
                    observation.predecessor_control_expected_native_turn_id
                ),
                expected_successor_delivery_id=(
                    observation.predecessor_successor_delivery_id
                ),
                expected_successor_turn_id=(
                    observation.predecessor_successor_turn_id
                ),
            )
        if turn is not None and turn.get("control_state") in {
            "pending",
            "interrupting",
            "waiting_terminal",
            "reconciling",
        }:
            await self._run_pending_interrupt(
                observation.session_id,
                str(turn["id"]),
            )
            return True
        if delivery is None:
            return False
        if delivery["state"] == "reserved":
            context = self._delivery_context(observation.session_id)
            self._hydrate_delivery_context(context, delivery)
            result = await self.deliver(
                self._request_from_delivery(
                    delivery,
                    has_attachments=bool(context.files),
                ),
                context=context,
            )
            # Recovery re-enters ``deliver`` behind the ingress handler's back,
            # so the receipt this admission earned has no other reporter.
            await self._report_delivery_receipts(
                observation.session_id,
                [delivery],
                state=result.state,
                admission=result.admission,
            )
            return result.state != "reserved"
        target_turn_id = str(delivery.get("current_target_turn_id") or "")
        if delivery["state"] == "pending_steer" and target_turn_id:
            await self._run_pending_steers(
                observation.session_id,
                target_turn_id,
                self._delivery_context(observation.session_id),
            )
            return True
        if delivery["state"] in {"steering", "reconciling_steer"}:
            if (
                delivery["state"] == "reconciling_steer"
                and delivery.get("current_receipt_outcome") == "accepted"
            ):
                result = await self._finish_steer(
                    str(delivery["id"]),
                    _accepted_steer_receipt(delivery),
                    context=None,
                )
                return result.state != "reconciling_steer"
            attempt_id = str(delivery.get("current_attempt_id") or "")
            expected_native = str(
                delivery.get("current_expected_native_turn_id") or ""
            )
            if not target_turn_id or not attempt_id or not expected_native:
                return False
            with self._sqlite_engine().connect() as conn:
                target_turn = delivery_store.get_turn(conn, target_turn_id)
            if target_turn is None:
                return False
            receipt = await self._reconcile_steer_attempt(
                str(target_turn["backend"]),
                SteerReconcileRequest(
                    target_session_id=observation.session_id,
                    expected_logical_turn_id=target_turn_id,
                    expected_native_turn_id=expected_native,
                    attempt_id=attempt_id,
                ),
            )
            outcome = getattr(receipt, "outcome", SteerOutcome.UNKNOWN)
            if (
                str(getattr(outcome, "value", outcome))
                == SteerOutcome.UNKNOWN.value
                and delivery["state"] == "reconciling_steer"
            ):
                return False
            result = await self._finish_steer(
                str(delivery["id"]),
                receipt,
                context=None,
            )
            return result.state != "reconciling_steer"
        return False

    def _turn_origin_native_message_id(self, turn_id: str) -> str:
        """The message a terminal receipt for one Turn belongs on, or ``""``.

        Read durably rather than from the live indicator because the caller runs
        AFTER a restart, where no in-memory handle survived. The Delivery's own
        snapshot is NOT the source: admission materializes it into ``messages``
        and clears ``snapshot_json``, so by the time a Turn is active its
        Delivery no longer carries the native id — only the ledger row it points
        at does. The snapshot is still consulted first for a Delivery caught
        before materialization.

        The target is not always the sender's message: a quick-reply callback is
        admitted with no ``native_message_id`` on purpose and wears its indicator
        on the bot echo instead, so ``_delivery_ack_target`` — which recovers
        that echo id from the durable admission context — decides first. Reading
        only the native id would return ``""`` for those turns and silently skip
        the ⚠️, leaving the echo claiming the turn is still running.
        """

        if not turn_id or not self._durable_schema_available():
            return ""
        try:
            with self._sqlite_engine().connect() as conn:
                turn = delivery_store.get_turn(conn, turn_id)
                if turn is None:
                    return ""
                delivery = delivery_store.get_delivery(
                    conn,
                    str(turn["initial_delivery_id"]),
                )
                if delivery is None:
                    return ""
                ack_target = self._delivery_ack_target(delivery)
                if ack_target:
                    return str(ack_target).strip()
                message_id = str(delivery.get("message_id") or "").strip()
                if not message_id:
                    return ""
                message = messages_service.get_message(conn, message_id)
                if message is None:
                    return ""
                return str(message.get("native_message_id") or "").strip()
        except Exception:
            logger.debug(
                "turn origin lookup failed for turn=%s", turn_id, exc_info=True
            )
            return ""

    def _controller_language(self) -> str:
        language_getter = getattr(self.controller, "_get_lang", None)
        if callable(language_getter):
            return language_getter()
        return getattr(getattr(self.controller, "config", None), "language", "en")

    async def _report_lost_im_turn(
        self,
        session_id: str,
        origin_native_message_id: str,
    ) -> None:
        """Tell an IM turn's author that its runtime died with the service.

        An IM turn owns no ``agent_runs`` row, so the Harness interruption lane is
        structurally unreachable for it: notices are stamped on runs, and
        ``_settle_agent_run_ids`` returns early for a Turn that has none. Without
        this report the turn's only trace is a durable row the user cannot read —
        the thread simply stops, which is indistinguishable from an agent that
        chose to stay quiet. The reported field case had a user wait five hours
        before asking what had happened to their request.

        This is deliberately NOT the shape of a Stop, which stays silent because
        the user caused it and already knows. Nobody asked for this ending, so it
        has to announce itself.

        Recovery runs from ``_on_runtime_ready``, which fires BEFORE an external
        transport has necessarily connected. A turn is terminal once reported, so
        a lost report is lost for good — hence the report is held until
        ``notify_transport_ready`` says that platform can actually deliver, and a
        send that still fails goes back on the queue rather than being dropped.
        ``avibe`` is ready as soon as the runtime is, so Workbench sessions
        report inline.
        """

        if self.controller is None:
            return
        try:
            context = self._delivery_context(session_id)
        except Exception:
            logger.debug(
                "lost turn report: no delivery context for session=%s",
                session_id,
                exc_info=True,
            )
            return
        platform = str(getattr(context, "platform", "") or "")
        if self._transport_can_deliver(platform) and await self._emit_lost_turn_report(
            context, session_id, origin_native_message_id
        ):
            return
        self._pending_lost_turn_reports.setdefault(platform, []).append(
            (session_id, str(origin_native_message_id or ""))
        )
        logger.info(
            "lost turn report held until %s transport can deliver (session=%s)",
            platform,
            session_id,
        )
        if self._transport_can_deliver(platform):
            # Held despite a ready transport means the send itself failed, so no
            # ready callback is coming to flush it — retry on our own clock.
            self._schedule_lost_turn_retry(platform)

    def _transport_can_deliver(self, platform: str) -> bool:
        """Whether ``platform`` can deliver right now.

        Unknown readiness is treated as ready: a controller that does not expose
        the probe (tests, embedded runners) must not silently swallow reports.
        """

        probe = getattr(self.controller, "is_im_transport_ready", None)
        if not callable(probe) or not platform:
            return True
        try:
            return bool(probe(platform))
        except Exception:
            logger.debug(
                "transport readiness probe failed for %s", platform, exc_info=True
            )
            return True

    async def notify_transport_ready(self, platform: str) -> int:
        """Flush the interruption reports held for one platform.

        Held in memory only: the turn is already terminal, so a report that never
        drains (transport disabled before it connects) is dropped rather than
        replayed on the next start, where it would be stale news. A send that
        fails against a connected transport is retained AND retried on a bounded
        backoff — see ``_schedule_lost_turn_retry`` for why nothing else would.
        """

        pending = self._pending_lost_turn_reports.pop(platform, [])
        if not pending or self.controller is None:
            return 0
        reported = 0
        unsent: list[tuple[str, str]] = []
        for session_id, origin_native_message_id in pending:
            try:
                context = self._delivery_context(session_id)
            except Exception:
                logger.debug(
                    "deferred lost turn report: no delivery context for session=%s",
                    session_id,
                    exc_info=True,
                )
                continue
            if await self._emit_lost_turn_report(
                context, session_id, origin_native_message_id
            ):
                reported += 1
            else:
                # "Ready" is the transport's claim, not a delivered message: a
                # transient API error still loses the notice. Popping happened
                # first, so an unsent report has to be put BACK or the only
                # record of the interruption is gone for the process's lifetime.
                unsent.append((session_id, str(origin_native_message_id or "")))
        if unsent:
            self._pending_lost_turn_reports.setdefault(platform, []).extend(unsent)
            logger.info(
                "%d lost turn report(s) on %s still undelivered; retrying",
                len(unsent),
                platform,
            )
            self._schedule_lost_turn_retry(platform)
        return reported

    def _schedule_lost_turn_retry(self, platform: str) -> None:
        """Start the bounded retry for reports this platform failed to deliver.

        ``notify_transport_ready`` has exactly one caller — ``_on_im_ready`` —
        and ``MultiIMClient`` suppresses further ready callbacks until the
        platform goes unready again. So a connection that merely hit one API
        error would hold the notice forever with nothing to nudge it: the retry
        has to come from here. One task per platform, a few attempts, then give
        up loudly — the next genuine reconnect flushes whatever is left.
        """

        existing = self._lost_turn_retry_tasks.get(platform)
        if existing is not None and not existing.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("no running loop for lost turn retry on %s", platform)
            return
        self._lost_turn_retry_tasks[platform] = loop.create_task(
            self._retry_lost_turn_reports(platform),
            name=f"lost-turn-report-retry:{platform}",
        )

    async def _retry_lost_turn_reports(self, platform: str) -> None:
        try:
            for delay in self.LOST_TURN_RETRY_DELAYS:
                await asyncio.sleep(delay)
                if not self._pending_lost_turn_reports.get(platform):
                    return
                if not self._transport_can_deliver(platform):
                    # Went unready again; the reconnect's ready callback flushes.
                    return
                await self.notify_transport_ready(platform)
            remaining = len(self._pending_lost_turn_reports.get(platform) or [])
            if remaining:
                logger.warning(
                    "%d lost turn report(s) on %s undelivered after %d retries; "
                    "held until the transport reconnects",
                    remaining,
                    platform,
                    len(self.LOST_TURN_RETRY_DELAYS),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("lost turn report retry failed for %s", platform, exc_info=True)
        finally:
            if self._lost_turn_retry_tasks.get(platform) is asyncio.current_task():
                self._lost_turn_retry_tasks.pop(platform, None)

    async def _emit_lost_turn_report(
        self,
        context: "MessageContext",
        session_id: str,
        origin_native_message_id: str,
    ) -> bool:
        """Emit one interruption notice. ``False`` means it did NOT reach the user.

        The dispatcher returns the delivered message id, and ``None`` when every
        send failed — so a falsy return is real evidence of loss, not merely an
        absent receipt. Treating it as success would stamp a ⚠️ next to a notice
        nobody got, on a turn that is already terminal and will never be retried
        by anything else.
        """

        try:
            delivered = await self.controller.emit_agent_message(
                context,
                "notify",
                i18n_t("turn.interrupted.serviceRestart", self._controller_language()),
            )
        except Exception:
            logger.warning(
                "lost turn report: failed to notify session=%s",
                session_id,
                exc_info=True,
            )
            return False
        if not delivered:
            logger.warning(
                "lost turn report: notify produced no delivery for session=%s",
                session_id,
            )
            return False
        # The dead process could not clear its own 👀. Retire it here so the
        # triggering message stops claiming the turn is still running.
        native_message_id = str(origin_native_message_id or "")
        service = getattr(self.controller, "processing_indicator", None)
        stamp = getattr(service, "stamp_orphaned_terminal_reaction", None)
        if not native_message_id or not callable(stamp):
            return True
        try:
            await stamp(context, native_message_id, INTERRUPTED_REACTION_EMOJI)
        except Exception:
            logger.debug(
                "lost turn report: terminal reaction failed for session=%s",
                session_id,
                exc_info=True,
            )
        return True

    async def recover_durable_delivery_state(
        self,
        session_id: str | None = None,
        *,
        service_restart: bool = False,
    ) -> list[str]:
        """Restore exact owners after backend restore, then project status.

        ``service_restart`` is the proof that process-bound Claude/Codex runtimes
        cannot still be executing in another live controller recovery pass.
        """

        with self._sqlite_engine().connect() as conn:
            turns = delivery_store.recovery_turns(conn, session_id)
        pending_interrupts: list[tuple[str, str]] = []
        pending_steers: list[tuple[str, str]] = []
        unresolved_starts: list[tuple[str, str, str]] = []
        lost_active_turns: list[tuple[str, str, str, str]] = []
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
                if turn["state"] == "waiting" and latest["state"] == "starting":
                    # This successor was activated by an earlier reconciliation in
                    # this same pass. Its native start is live now, not crash residue.
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
                    if (
                        bound is None
                        and latest["state"] == "active"
                        and latest.get("start_receipt_outcome") == "accepted"
                        and str(latest.get("backend") or "") == "opencode"
                        and _same_opencode_native_session(
                            latest.get("native_turn_id"),
                            identity[1],
                        )
                    ):
                        bound = delivery_store.rebind_restored_native_generation(
                            conn,
                            turn_id,
                            expected_version=int(latest["version"]),
                            expected_native_turn_id=str(latest["native_turn_id"]),
                            restored_native_turn_id=identity[1],
                        )
                    if bound is not None:
                        initial_batch = delivery_store.initial_deliveries_for_turn(
                            conn, turn_id
                        )
                        if initial_batch and all(
                            delivery["state"] == "claimed"
                            for delivery in initial_batch
                        ):
                            materialized = delivery_store.materialize_start_acceptance(
                                conn,
                                turn_id=turn_id,
                                evidence={"kind": "restored_native_identity", "native_turn_id": identity[1]},
                            )
                            if materialized:
                                materialized_ids.add(
                                    str(
                                        materialized[0].get("message_id")
                                        or materialized[0]["id"]
                                    )
                                )
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
                if latest["state"] == "starting" and latest.get(
                    "start_receipt_outcome"
                ) is None:
                    latest = delivery_store.mark_start_unknown(
                        conn,
                        turn_id,
                        expected_version=int(latest["version"]),
                        receipt={"reason": "restart_without_native_evidence"},
                    )
                if (
                    latest is not None
                    and latest["state"] == "starting"
                    and latest.get("start_receipt_outcome") == "unknown"
                    and str(latest.get("backend") or "")
                    in _NON_RESTORABLE_RUNTIME_BACKENDS
                ):
                    unresolved_starts.append(
                        (
                            target_session,
                            turn_id,
                            str(latest.get("start_attempt_id") or ""),
                        )
                    )
                elif (
                    latest is not None
                    and latest["state"] == "active"
                    and latest.get("start_receipt_outcome") == "accepted"
                    and service_restart
                    and str(latest.get("backend") or "")
                    in _NON_RESTORABLE_RUNTIME_BACKENDS
                ):
                    lost_active_turns.append(
                        (
                            target_session,
                            turn_id,
                            str(latest.get("start_attempt_id") or ""),
                            str(latest.get("backend") or ""),
                        )
                    )

        for target_session, turn_id, attempt_id, backend in lost_active_turns:
            # Read Run attribution BEFORE terminalizing: the notice below is owed
            # only to a turn that has none, and terminalization retires the
            # deliveries the attribution is derived from.
            owning_run_ids = self.accepted_agent_run_ids_for_turn(turn_id)
            origin_message_id = self._turn_origin_native_message_id(turn_id)
            terminal = self._terminalize_durable_turn(
                turn_id,
                "failed",
                settled_by=SETTLED_BY_NO_TERMINAL_RESULT,
                evidence_kind="restart_runtime_missing",
                evidence={
                    "backend": backend,
                    "reason": "accepted_turn_runtime_not_restorable",
                },
                expected_start_attempt_id=attempt_id,
            )
            if not terminal.get("changed"):
                continue
            recovered.append(target_session)
            if not owning_run_ids:
                await self._report_lost_im_turn(target_session, origin_message_id)
            successor_turn_id = str(terminal.get("successor_turn_id") or "")
            if successor_turn_id:
                await self._start_persisted_turn(successor_turn_id)

        for target_session, turn_id, attempt_id in unresolved_starts:
            terminal = self._terminalize_durable_turn(
                turn_id,
                "failed",
                settled_by="restart_unknown_start",
                evidence_kind="restart_unknown_start",
                evidence={
                    "reason": "native_start_acceptance_unrecoverable",
                    "automatic_replay_limit": _MAX_AUTOMATIC_UNKNOWN_START_REPLAYS,
                },
                expected_start_attempt_id=attempt_id,
                replay_unknown_start=True,
            )
            if not terminal.get("changed"):
                continue
            recovered.append(target_session)
            retry_turn_id = str(terminal.get("successor_turn_id") or "")
            if retry_turn_id:
                await self._start_persisted_turn(retry_turn_id)

        with self._sqlite_engine().connect() as conn:
            unresolved = delivery_store.unresolved_deliveries(conn, session_id)
            accepted_steer_attempts: list[dict[str, Any]] = []
            steer_attempts: list[tuple[dict[str, Any], str]] = []
            seen_attempt_ids: set[str] = set()
            for attempt in unresolved:
                if attempt["state"] not in {"steering", "reconciling_steer"}:
                    continue
                attempt_id = str(attempt.get("current_attempt_id") or "")
                target_turn_id = str(attempt.get("current_target_turn_id") or "")
                expected_native_id = str(
                    attempt.get("current_expected_native_turn_id") or ""
                )
                if (
                    not attempt_id
                    or attempt_id in seen_attempt_ids
                    or not target_turn_id
                ):
                    continue
                target_turn = delivery_store.get_turn(conn, target_turn_id)
                if target_turn is None:
                    continue
                seen_attempt_ids.add(attempt_id)
                if (
                    attempt["state"] == "reconciling_steer"
                    and attempt.get("current_receipt_outcome") == "accepted"
                ):
                    accepted_steer_attempts.append(attempt)
                    continue
                if not expected_native_id:
                    continue
                steer_attempts.append((attempt, str(target_turn["backend"])))

        for attempt in accepted_steer_attempts:
            result = await self._finish_steer(
                str(attempt["id"]),
                _accepted_steer_receipt(attempt),
                context=None,
            )
            if result.state != "reconciling_steer":
                recovered.append(str(attempt["session_id"]))

        for attempt, backend in steer_attempts:
            attempt_id = str(attempt["current_attempt_id"])
            target_turn_id = str(attempt["current_target_turn_id"])
            receipt = await self._reconcile_steer_attempt(
                backend,
                SteerReconcileRequest(
                    target_session_id=str(attempt["session_id"]),
                    expected_logical_turn_id=target_turn_id,
                    expected_native_turn_id=str(
                        attempt["current_expected_native_turn_id"]
                    ),
                    attempt_id=attempt_id,
                ),
            )
            outcome = getattr(receipt, "outcome", SteerOutcome.UNKNOWN)
            if (
                str(getattr(outcome, "value", outcome)) == SteerOutcome.UNKNOWN.value
                and attempt["state"] == "reconciling_steer"
            ):
                continue
            result = await self._finish_steer(
                str(attempt["id"]),
                receipt,
                context=None,
            )
            if result.state != "reconciling_steer":
                recovered.append(str(attempt["session_id"]))

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
            with self._sqlite_engine().begin() as conn:
                reserve_write_lock(conn)
                latest = delivery_store.get_delivery(conn, str(reservation["id"]))
                if latest is None or latest["state"] != "reserved":
                    continue
                retired = False
                if not self._has_resolvable_delivery_input(conn, latest):
                    retired = delivery_store.retire_reserved(
                        conn,
                        target_session,
                        str(latest["id"]),
                        reason="empty_or_invalid_reserved_submission",
                    )
                    if retired:
                        retired_delivery = delivery_store.get_delivery(
                            conn,
                            str(latest["id"]),
                        )
                        self._cancel_runs_for_retired_delivery(conn, retired_delivery)
                else:
                    reservation = latest
            if retired:
                logger.warning(
                    "retired reserved Delivery=%s because it has no resolvable input",
                    reservation["id"],
                )
                recovered.append(target_session)
                continue
            try:
                context = self._delivery_context(target_session)
                self._hydrate_delivery_context(context, reservation)
                result = await self.deliver(
                    self._request_from_delivery(
                        reservation,
                        has_attachments=bool(context.files),
                    ),
                    context=context,
                )
            except Exception:
                logger.exception(
                    "failed to recover reserved Delivery=%s",
                    reservation["id"],
                )
            else:
                # Same as the observation path: nothing else reports the
                # admission outcome of a Delivery revived by recovery.
                await self._report_delivery_receipts(
                    target_session,
                    [reservation],
                    state=result.state,
                    admission=result.admission,
                )
                if result.state != "reserved":
                    recovered.append(target_session)
        with self._sqlite_engine().connect() as conn:
            terminal_run_owners = delivery_store.terminal_turn_agent_run_owners(
                conn,
                session_id,
            )
            all_run_ids = {
                run_id
                for _terminal_turn, run_ids in terminal_run_owners
                for run_id in run_ids
            }
            run_rows = {
                str(row["id"]): row
                for row in conn.execute(
                    select(
                        agent_runs.c.id,
                        agent_runs.c.status,
                        agent_runs.c.metadata_json,
                    ).where(
                        agent_runs.c.id.in_(all_run_ids)
                    )
                ).mappings()
            }
            recoverable_owners: list[tuple[dict[str, Any], list[str]]] = []
            for terminal_turn, run_ids in terminal_run_owners:
                unsettled = [
                    run_id
                    for run_id in run_ids
                    if run_id in run_rows
                    and normalize_run_status(run_rows[run_id]["status"])
                    in {"queued", "running"}
                ]
                settled_by = str(terminal_turn.get("settled_by") or "")
                legacy_pending_notice = False
                if settled_by in SETTLEMENTS_WITHOUT_RESULT:
                    for run_id in run_ids:
                        row = run_rows.get(run_id)
                        if row is None:
                            continue
                        try:
                            metadata = json.loads(str(row["metadata_json"] or "{}"))
                        except (TypeError, ValueError, json.JSONDecodeError):
                            continue
                        notice = (
                            metadata.get(OWED_FAILURE_NOTICE_KEY)
                            if isinstance(metadata, dict)
                            else None
                        )
                        if (
                            isinstance(notice, dict)
                            and notice.get("state") == "pending"
                            and not str(notice.get("turn_id") or "").strip()
                            and str(
                                notice.get("interrupt_reason")
                                or metadata.get("interrupt_reason")
                                or ""
                            ).strip()
                            == settled_by
                        ):
                            legacy_pending_notice = True
                            break
                if unsettled or legacy_pending_notice:
                    # Older releases stamped one notice per Run even though the
                    # accepted Delivery relation retained this exact Turn owner.
                    recoverable_owners.append((terminal_turn, run_ids))
        for terminal_turn, run_ids in recoverable_owners:
            self._settle_agent_run_ids_from_terminal_turn(run_ids, terminal_turn)
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

    @staticmethod
    def _activate_linked_waiting_successor(
        conn: Connection,
        *,
        owner: _RuntimeStartOwner,
        predecessor: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Claim the exact linked successor only behind a terminal winner."""

        if (
            predecessor is None
            or predecessor.get("state") != "terminal"
            or predecessor.get("control_mode") != "replace"
            or predecessor.get("control_state")
            not in {"pending", "interrupting", "waiting_terminal", "reconciling"}
        ):
            return None
        successor_id = str(predecessor.get("control_successor_turn_id") or "")
        delivery_id = str(predecessor.get("control_successor_delivery_id") or "")
        if not successor_id or not delivery_id:
            return None
        successor = delivery_store.get_turn(conn, successor_id)
        delivery = delivery_store.get_delivery(conn, delivery_id)
        if (
            successor is None
            or delivery is None
            or str(successor.get("session_id") or "")
            != str(predecessor.get("session_id") or "")
            or str(successor.get("initial_delivery_id") or "") != delivery_id
            or str(delivery.get("session_id") or "")
            != str(predecessor.get("session_id") or "")
            or str(delivery.get("turn_id") or "") != successor_id
            or str(delivery.get("turn_role") or "") != "initial"
            or delivery.get("turn_position") != 0
        ):
            return None
        binding = conn.execute(
            select(
                agent_sessions.c.agent_backend,
                agent_sessions.c.session_anchor,
                agent_sessions.c.workdir,
            ).where(agent_sessions.c.id == str(successor["session_id"]))
        ).mappings().one_or_none()
        if (
            owner.session_id != str(successor["session_id"])
            or (owner.backend and owner.backend != str(successor["backend"]))
            or not owner.admitted
            or binding is None
            or str(binding["agent_backend"] or "").strip() != owner.backend
            or str(binding["session_anchor"] or "").strip() != owner.session_anchor
            or binding["workdir"] != owner.workdir
        ):
            return None
        return delivery_store.activate_waiting_successor(
            conn,
            turn=successor,
            delivery=delivery,
        )

    async def _resume_linked_control_successor(
        self,
        session_id: str,
        terminal_turn_id: str,
        *,
        observation: RuntimeDeliveryObservation | None = None,
    ) -> str | None:
        """Start only the exact replacement linked from a settled control Turn."""
        start_attempt_id = ""
        with self._sqlite_engine().connect() as conn:
            observed_terminal = delivery_store.get_turn(conn, terminal_turn_id)
            observed_successor = delivery_store.get_turn(
                conn,
                str((observed_terminal or {}).get("control_successor_turn_id") or ""),
            )
        owner_backend = str(
            (observed_successor or observed_terminal or {}).get("backend") or ""
        )
        with self._runtime_start_owner(session_id, owner_backend) as start_owner, self._sqlite_engine().begin() as conn:
            reserve_write_lock(conn)
            terminal = delivery_store.get_turn(conn, terminal_turn_id)
            successor_id = str(
                (terminal or {}).get("control_successor_turn_id") or ""
            )
            successor_delivery_id = str(
                (terminal or {}).get("control_successor_delivery_id") or ""
            )
            predecessors = list(
                conn.execute(
                    select(session_turn_rows.c.id).where(
                        session_turn_rows.c.control_successor_turn_id == successor_id,
                        session_turn_rows.c.control_successor_delivery_id
                        == successor_delivery_id,
                    )
                ).scalars()
            )
            session_status = conn.execute(
                select(agent_sessions.c.status)
                .where(agent_sessions.c.id == session_id)
                .limit(1)
            ).scalar_one_or_none()
            if (
                terminal is None
                or terminal["state"] != "terminal"
                or str(terminal["session_id"]) != session_id
                or terminal.get("control_mode") != "replace"
                or not successor_id
                or not successor_delivery_id
                or predecessors != [terminal_turn_id]
                or session_status != "active"
                or (
                    observation is not None
                    and not self._terminal_predecessor_matches_observation(
                        terminal,
                        observation,
                    )
                )
            ):
                return None
            successor = delivery_store.get_turn(conn, successor_id)
            successor_delivery = delivery_store.get_delivery(
                conn, successor_delivery_id
            )
            if (
                successor is None
                or successor_delivery is None
                or str(successor.get("session_id") or "") != session_id
                or str(successor.get("initial_delivery_id") or "")
                != successor_delivery_id
                or str(successor_delivery.get("session_id") or "") != session_id
                or str(successor_delivery.get("turn_id") or "") != successor_id
                or str(successor_delivery.get("turn_role") or "") != "initial"
                or successor_delivery.get("turn_position") != 0
                or (
                    observation is not None
                    and (
                        str(successor.get("id") or "")
                        != str(observation.turn_id or "")
                        or str(successor.get("state") or "")
                        != str(observation.turn_state or "")
                        or int(successor.get("version") or 0)
                        != observation.turn_version
                        or str(successor.get("start_attempt_id") or "")
                        != str(observation.start_attempt_id or "")
                        or str(successor.get("native_turn_id") or "")
                        != str(observation.native_turn_id or "")
                        or str(successor_delivery.get("id") or "")
                        != str(observation.delivery_id or "")
                        or str(successor_delivery.get("state") or "")
                        != str(observation.delivery_state or "")
                        or int(successor_delivery.get("version") or 0)
                        != observation.delivery_version
                        or str(successor_delivery.get("current_attempt_id") or "")
                        != str(observation.delivery_attempt_id or "")
                        or str(
                            successor_delivery.get("current_target_turn_id") or ""
                        )
                        != str(observation.delivery_target_turn_id or "")
                        or str(
                            successor_delivery.get(
                                "current_expected_native_turn_id"
                            )
                            or ""
                        )
                        != str(observation.delivery_expected_native_turn_id or "")
                    )
                )
            ):
                return None
            if successor["state"] == "waiting":
                started = self._activate_linked_waiting_successor(
                    conn,
                    owner=start_owner,
                    predecessor=terminal,
                )
                if started is None:
                    return None
                successor = started
            elif successor["state"] != "starting" or successor_delivery["state"] != "claimed":
                return None
            start_attempt_id = str(successor.get("start_attempt_id") or "")
            if not start_attempt_id:
                raise RuntimeError("linked successor activation has no start attempt")
            if terminal.get("control_state") in {
                "pending",
                "interrupting",
                "waiting_terminal",
                "reconciling",
            }:
                settled = delivery_store.cas_turn(
                    conn,
                    terminal_turn_id,
                    expected_version=int(terminal["version"]),
                    expected_states=("terminal",),
                    values={
                        "control_state": "settled",
                        "control_receipt_outcome": (
                            terminal.get("control_receipt_outcome") or "accepted"
                        ),
                    },
                )
                if settled is None:
                    raise RuntimeError("linked successor terminal resume lost")
        await self._start_persisted_turn(
            successor_id,
            expected_start_attempt_id=start_attempt_id,
        )
        return successor_id

    async def _resume_post_terminal(self, session_id: str) -> None:
        with self._sqlite_engine().begin() as conn:
            reserve_write_lock(conn)
            owner = delivery_store.active_turn(conn, session_id)
        if owner is not None:
            await self._resume_durable_session(session_id)
        else:
            await self.drain_delivery_queue(session_id)

    def _turn_has_terminal_run(self, turn_id: str) -> bool:
        with self._sqlite_engine().connect() as conn:
            deliveries = delivery_store.initial_deliveries_for_turn(conn, turn_id)
            run_ids = list(
                dict.fromkeys(
                    run_id
                    for delivery in deliveries
                    for run_id in delivery_store.agent_run_ids_for_delivery(
                        conn, delivery
                    )
                )
            )
            if not run_ids:
                return False
            statuses = {
                str(row["id"]): normalize_run_status(row["status"])
                for row in conn.execute(
                    select(agent_runs.c.id, agent_runs.c.status).where(
                        agent_runs.c.id.in_(run_ids)
                    )
                ).mappings()
            }
        return any(
            statuses.get(run_id) not in {"queued", "running"}
            for run_id in run_ids
        )

    async def submit(
        self,
        session_id: Optional[str],
        context: "MessageContext",
        text: str,
        *,
        source: str = SOURCE_HUMAN,
        delivery_intent: Literal["queue", "replace", "steer"] = "queue",
    ) -> TurnSubmissionResult:
        """Admit one caller through the durable Delivery owner."""
        from core.message_priority import priority_for_delivery_intent

        priority = priority_for_delivery_intent(delivery_intent)
        if not (isinstance(session_id, str) and session_id):
            # No session key (CLI-style) — just run; nothing to queue against.
            await self._run(None, context, text, source=source)
            return TurnSubmissionResult(
                route="ran",
                delivery_status="ran" if delivery_intent == "replace" else None,
            )

        spec = dict(getattr(context, "platform_specific", None) or {})
        dispatch_text = text
        scheduled_metadata = (
            dict(spec.get("message_metadata") or {})
            if isinstance(spec.get("message_metadata"), dict)
            else {}
        )
        if source == SOURCE_SCHEDULED:
            scheduled_metadata[SCHEDULED_PROVENANCE_KEY] = capture_scheduled_provenance(
                context
            )
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
            priority=priority,
            content=dispatch_text,
            has_content=delivery_store.has_substantive_input(
                text,
                has_attachments=bool(getattr(context, "files", None)),
            ),
            delivery_id=delivery_id,
            scope_id=str(spec.get("scope_id") or "").strip() or None,
            platform=str(getattr(context, "platform", None) or "avibe"),
            source=source_value,
            author="harness" if source == SOURCE_SCHEDULED else "user",
            message_type="harness" if source == SOURCE_SCHEDULED else "user",
            display_text=str(spec.get("display_text") or text),
            content_json=spec.get("message_content") if isinstance(spec.get("message_content"), dict) else None,
            metadata=scheduled_metadata,
            author_id=str(
                spec.get("author_id")
                or (spec.get("task_definition_id") if source == SOURCE_SCHEDULED else "")
                or ""
            ).strip() or None,
            author_name=str(
                spec.get("author_name")
                or (spec.get("task_trigger_kind") if source == SOURCE_SCHEDULED else "")
                or ""
            ).strip() or None,
            native_message_id=native_message_id,
            message_kind=str(
                spec.get("message_kind")
                or (
                    "original"
                    if getattr(context, "is_original_human_text", None) is True
                    or getattr(context, "is_original_human_attachment", None) is True
                    else getattr(context, "message_kind", "unknown")
                )
            ),
        )
        result = await self.deliver(request, context=context)
        enqueued_states = {
            "queued",
            "pending_steer",
            "steering",
            "reconciling_steer",
            "interrupt_waiting",
        }
        enqueued = result.state in enqueued_states
        if enqueued:
            from core.inbox_events import bus

            bus.publish("queue.updated", {"session_id": session_id})
        return TurnSubmissionResult(
            route="enqueued" if enqueued else "ran",
            queue_persisted=True,
            target_was_busy=busy,
            delivery_status=result.state if delivery_intent == "replace" else None,
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
        lifecycle_snapshot: object | None = None,
    ) -> None:
        """Start a fire-and-forget turn and HOLD it open until it settles.

        A no-op chunk sink keeps ``dispatch_turn`` alive for the turn's lifetime so
        ``in_flight`` stays populated (Stop works) and the session-level
        ``turn.start`` / ``turn.end`` lifecycle is published for the browser's
        working indicator. After every definitive terminal outcome, the oldest
        compatible queued segment starts immediately. A real safety fence can defer
        that resume, but an idle Session never keeps claimable backlog by policy.

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
            nonlocal lifecycle_snapshot
            cancelled = False
            failed = False
            prewrite_refused = False
            definitive_prewrite_exit = False
            # How this turn's waiter was released, in the ``core.run_settlement``
            # vocabulary. Anything other than a real terminal result means no result
            # is coming, so an ``agent_runs`` row this turn owns has to be settled
            # here — the gate lane returns to ``_execute_agent_run`` long before the
            # turn ends, so nobody downstream can do it (Codex P1).
            settled_by: Optional[str] = None
            try:
                if (
                    durable_turn_registered
                    and logical_turn_id
                    and self._turn_has_terminal_run(logical_turn_id)
                ):
                    prewrite_refused = True
                    self._terminalize_durable_turn(
                        logical_turn_id,
                        "not_written",
                        settled_by="terminal_run",
                        evidence_kind="terminal_run_before_native_dispatch",
                    )
                    return
                snapshot_options = (
                    {"lifecycle_snapshot": lifecycle_snapshot}
                    if lifecycle_snapshot is not None
                    else {}
                )
                dispatch = dispatch_turn_with_outcome(
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
                    **snapshot_options,
                )
                snapshot_options.clear()
                lifecycle_snapshot = None
                outcome = await dispatch
                settled_by = outcome.settled_by
                definitive_prewrite_exit = outcome.backend_dispatch_attempted is False
                current = self.in_flight.get(str(session_id or ""))
                if (
                    definitive_prewrite_exit
                    and current is not None
                    and current.task is asyncio.current_task()
                    and current.logical_turn_id == logical_turn_id
                    and current.cancel_settled_by == SETTLED_BY_STOPPED
                ):
                    # OpenCode intentionally absorbs the inner cancellation after
                    # cleaning up its request. Preserve the Stop attribution at
                    # this outer durable boundary so the input is retired instead
                    # of becoming a queued retry.
                    settled_by = SETTLED_BY_STOPPED
            except asyncio.CancelledError:
                cancelled = True
                # Do NOT decide the reason here: the canceller knows it, and it is
                # recorded on the Turn (``cancel_settled_by``) which is only popped
                # in the ``finally`` below. The Stop path sets ``stopped`` before
                # cancellation; an unrelated task cancellation remains unknown and
                # must not release queued work as though the user had stopped it.
                raise
            except Exception:
                # Preserve the exact boundary phase even when the handler's own
                # terminal-error persistence raises. A proven pre-write failure is
                # safe to requeue; anything at or beyond adapter entry is logged and
                # retained as an unknown start instead of guessing that it was absent.
                definitive_prewrite_exit = backend_dispatch_attempted(context) is False
                failed = not definitive_prewrite_exit
                settled_by = SETTLED_BY_NO_TERMINAL_RESULT
                logger.exception("internal async dispatch failed for session=%s", session_id)
            finally:
                if isinstance(session_id, str):
                    durable_terminal_result: dict[str, Any] = {}
                    prewrite_refused = prewrite_refused or (
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
                    cancel_defers_queue_resume = bool(
                        turn is not None and turn.cancel_defers_queue_resume
                    )
                    if cancelled:
                        # Attribute the cancellation to whoever caused it. The Turn
                        # carries the cause when the canceller had a more specific one
                        # than an unrelated runner cancellation. Only an explicit
                        # Stop is allowed to project ``stopped`` and release backlog.
                        settled_by = (
                            getattr(turn, "cancel_settled_by", None) if turn is not None else None
                        ) or SETTLED_BY_NO_TERMINAL_RESULT
                    completion = self._settle_model_hub_turn(context, settled_by)
                    if inspect.isawaitable(completion):
                        from core.handlers.model_hub.async_owner import await_owned_task

                        try:
                            await await_owned_task(asyncio.ensure_future(completion))
                        except Exception:
                            logger.warning("Model Hub turn finalization failed", exc_info=True)
                    # Durable output may already have marked the old row terminal.
                    # _start_persisted_turn still fences its successor on this live
                    # runner, so release the projection only after Hub finalization.
                    if turn is not None and self.in_flight.get(session_id) is turn:
                        self.in_flight.pop(session_id, None)
                        bus.publish(
                            "turn.end",
                            _turn_event_payload(session_id, logical_turn_id),
                        )
                    if logical_turn_id and durable_turn_registered:
                        durable_terminal_result = self._reconcile_durable_runner_release(
                            logical_turn_id,
                            cancelled=cancelled,
                            failed=failed,
                            prewrite_refused=prewrite_refused,
                            definitive_prewrite_exit=definitive_prewrite_exit,
                            settled_by=settled_by,
                            terminal_is_error=terminal_is_error,
                            cancel_defers_queue_resume=cancel_defers_queue_resume,
                        )
                    # Only definitive pre-write failure may synthesize an empty
                    # terminal result. Once native work may have produced output, a
                    # persistence failure leaves the durable Turn unresolved for
                    # exact reconciliation; an empty fallback would overwrite that
                    # evidence with a fabricated terminal response.
                    if (
                        definitive_prewrite_exit
                        and settled_by != SETTLED_BY_STOPPED
                    ):
                        try:
                            await self.controller.emit_agent_message(
                                context,
                                "result",
                                "",
                                is_error=True,
                                output=terminal_turn_output(),
                            )
                        except Exception:
                            logger.exception(
                                "failed to persist terminal dispatch error for session=%s",
                                session_id,
                            )
                    # Settle before flushing: the next turn must not start while a run
                    # this one owned is still ``running``. Placed after the failure
                    # emit above so the honest outbound terminal writes first and this
                    # guarded write degrades to a no-op.
                    self._settle_turn_owned_agent_runs(context, settled_by)
                    # A real start/reconciliation fence may defer resume. Stop,
                    # failure, and natural completion are all terminal and therefore
                    # all release the Session to its oldest claimable queue segment.
                    defer_durable_resume = bool(
                        durable_turn_registered
                        and durable_terminal_result.get("defer_queue_resume")
                    )
                    should_flush = not defer_durable_resume
                    backend = self._context_backend(context)
                    if should_flush and backend in self._draining_backends:
                        self._deferred_restart_sessions.setdefault(backend, set()).add(session_id)
                    elif should_flush:
                        if durable_turn_registered:
                            await self._resume_post_terminal(session_id)
                        else:
                            await self.flush_queue(session_id)
                    elif (
                        durable_turn_registered
                        and settled_by != SETTLED_BY_RESTARTED
                    ):
                        # Stop may persist the old Turn's terminal snapshot before
                        # releasing this runner. In that ordering the terminal CAS
                        # already activated the linked P0 successor, so this runner
                        # sees an idempotent no-op instead of the successor ID.
                        await self._resume_linked_control_successor(
                            session_id,
                            str(logical_turn_id or ""),
                        )

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
            bus.publish(
                "turn.start",
                _turn_event_payload(session_id, logical_turn_id),
            )

    async def flush_queue(self, session_id: str) -> bool:
        """Drain one claimable Delivery through the sole FIFO owner."""
        return await self.drain_delivery_queue(session_id)

    async def reconcile_terminal_run_delivery(
        self,
        run_id: str,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        """Retire an exact terminal Run input when no native effect is possible."""

        retired = False
        state: str | None = None
        with self._sqlite_engine().begin() as conn:
            reserve_write_lock(conn)
            run = conn.execute(
                select(
                    agent_runs.c.status,
                    agent_runs.c.session_id,
                    agent_runs.c.delivery_id,
                )
                .where(agent_runs.c.id == run_id)
                .limit(1)
            ).mappings().first()
            if (
                run is None
                or normalize_run_status(run["status"]) in {"queued", "running"}
                or str(run["session_id"] or "") != session_id
                or not run["delivery_id"]
            ):
                return {"changed": False, "state": None}
            delivery = delivery_store.get_delivery(conn, str(run["delivery_id"]))
            if delivery is None or str(delivery["session_id"]) != session_id:
                return {"changed": False, "state": None}
            state = str(delivery["state"])
            if delivery_store.policy_for(state).run_cancel == "retire":
                retired = self._retire_delivery_not_written(
                    conn,
                    session_id,
                    str(delivery["id"]),
                    reason="terminal_run_before_native_write",
                )
                if retired:
                    state = "retired"
        if retired:
            from core.inbox_events import bus

            bus.publish("queue.updated", {"session_id": session_id})
        return {"changed": retired, "state": state}

    async def recover_persisted_agent_run_queue(
        self,
        session_id: Optional[str] = None,
    ) -> list[str]:
        """Resume durable Workbench Agent Run queues after their owner vanished.

        The Delivery relation is the durable owner. A restart may resume its
        exact FIFO head only while the linked Agent Run remains nonterminal.
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
                    run_ids = delivery_store.agent_run_ids_for_delivery(conn, head)
                    run_id = run_ids[0] if run_ids else None
                    if (
                        not isinstance(spec, dict)
                        or spec.get("task_trigger_kind") != "agent_run"
                        or not run_id
                    ):
                        continue
                    run_row = conn.execute(
                        select(agent_runs.c.status, agent_runs.c.cancel_requested)
                        .where(agent_runs.c.id == run_id)
                    ).mappings().first()
                if (
                    run_row is None
                    or bool(run_row["cancel_requested"])
                    or normalize_run_status(run_row["status"]) not in {"queued", "running"}
                ):
                    continue
                if await self.drain_delivery_queue(queued_session_id):
                    recovered.append(queued_session_id)
        return recovered

    def _repair_ownerless_running_projection(self, session_id: str) -> bool:
        """Clear a legacy running projection only after an exact owner recheck."""

        engine = self._sqlite_engine()
        if not callable(getattr(engine, "connect", None)):
            return False
        if not self._durable_schema_available():
            return False
        with engine.begin() as conn:
            reserve_write_lock(conn)
            if delivery_store.active_turn(conn, session_id) is not None:
                return False
            repaired = conn.execute(
                update(agent_sessions)
                .where(agent_sessions.c.id == session_id)
                .where(agent_sessions.c.agent_status == "running")
                .values(agent_status="idle")
            )
            changed = bool(repaired.rowcount)
        if changed:
            from core.inbox_events import bus

            bus.publish(
                "session.status",
                {"session_id": session_id, "agent_status": "idle"},
            )
        return changed

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
            owner_run_ids = sorted(self._agent_run_ids_from_spec(payload))
            owner_run_id = str(payload.get("task_execution_id") or "").strip()
            if owner_run_id and owner_run_id not in owner_run_ids:
                owner_run_ids.insert(0, owner_run_id)
            if not owner_run_id and owner_run_ids:
                owner_run_id = owner_run_ids[0]
            owner = {
                "source": str(
                    payload.get("task_trigger_kind")
                    or payload.get("turn_source")
                    or ("agent_run" if payload.get("accepted_agent_run_ids") else "human")
                ),
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
                        for run_id in delivery_store.accepted_agent_run_ids_for_turn(
                            conn,
                            str(durable_turn["id"]),
                        ):
                            if run_id not in owner_run_ids:
                                owner_run_ids.append(run_id)
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
            "recovered_agent_status": (
                False
                if active
                else self._repair_ownerless_running_projection(session_id)
            ),
        }
        if backend:
            result["backend"] = backend
        if owner is not None:
            result["owner"] = owner
        return result

    async def release_for_service_shutdown(self) -> int:
        """Fail exact accepted Run owners without draining replacement work."""

        if not self._durable_schema_available():
            return 0

        def live_owners() -> dict[str, dict[str, Any]]:
            with self._sqlite_engine().connect() as conn:
                rows = conn.execute(
                    select(
                        session_turn_rows.c.id.label("turn_id"),
                        session_turn_rows.c.session_id,
                        session_turn_rows.c.control_mode,
                        agent_runs.c.cancel_requested,
                    )
                    .join(
                        delivery_rows,
                        delivery_rows.c.turn_id == session_turn_rows.c.id,
                    )
                    .join(
                        agent_runs,
                        agent_runs.c.delivery_id == delivery_rows.c.id,
                    )
                    .where(
                        session_turn_rows.c.state.in_(
                            delivery_store.TURN_OWNER_STATES
                        )
                    )
                    .where(delivery_rows.c.state == "accepted")
                    .where(
                        agent_runs.c.status.in_(
                            ["queued", "pending", "running", "processing"]
                        )
                    )
                ).mappings()
                owners: dict[str, dict[str, Any]] = {}
                for row in rows:
                    turn_id = str(row["turn_id"])
                    owner = owners.setdefault(
                        turn_id,
                        {
                            "turn_id": turn_id,
                            "session_id": str(row["session_id"]),
                            "control_mode": row["control_mode"],
                            "cancel_requested": False,
                        },
                    )
                    owner["cancel_requested"] = bool(
                        owner["cancel_requested"] or row["cancel_requested"]
                    )
                return owners

        owners = live_owners()
        done_tasks: list[asyncio.Task] = []
        for owner in owners.values():
            projected = self.in_flight.get(str(owner["session_id"]))
            if (
                projected is not None
                and projected.logical_turn_id == owner["turn_id"]
                and projected.task.done()
            ):
                done_tasks.append(projected.task)
        if done_tasks:
            await asyncio.gather(*done_tasks, return_exceptions=True)

        owners = live_owners()
        tasks_to_cancel: list[asyncio.Task] = []
        released_sessions: set[str] = set()
        for owner in owners.values():
            session_id = str(owner["session_id"])
            turn_id = str(owner["turn_id"])
            projected = self.in_flight.get(session_id)
            if projected is None or projected.logical_turn_id != turn_id:
                continue
            user_stopped = bool(
                owner["control_mode"] == "stop_only"
                or owner["cancel_requested"]
                or projected.cancel_settled_by == SETTLED_BY_STOPPED
            )
            settled_by = SETTLED_BY_STOPPED if user_stopped else SETTLED_BY_RESTARTED
            terminal = self._terminalize_durable_turn(
                turn_id,
                "canceled" if user_stopped else "failed",
                settled_by=settled_by,
                evidence_kind=(
                    "service_shutdown_after_user_stop"
                    if user_stopped
                    else "service_shutdown"
                ),
                evidence={"reason": "scheduled_service_shutdown"},
                resume_successors=False,
            )
            if terminal.get("changed"):
                released_sessions.add(session_id)
            projected.cancel_defers_queue_resume = True
            if not projected.task.done():
                projected.cancel_settled_by = settled_by
                projected.task.cancel()
                tasks_to_cancel.append(projected.task)

        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
        return len(released_sessions)

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
        legacy_projection_sessions: set[str] = set()
        tasks_to_settle: list[asyncio.Task] = []
        restored_owners: list[dict[str, Any]] = []
        if self._durable_schema_available():
            with self._sqlite_engine().connect() as conn:
                # Refresh owns exactly the generation that existed when draining
                # began. Cancelling one of these Turns may activate a replacement;
                # that successor belongs to the post-refresh generation and must
                # remain deferred until the backend reopens.
                restored_owners = delivery_store.live_turns_for_backend_sessions(
                    conn,
                    backend,
                    base_session_ids,
                )
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
            # Record the cause BEFORE cancelling: this is a runtime refresh, not a
            # user Stop, so a scheduled run this turn owns must not settle as
            # ``canceled`` with the user-stop explanation (Codex P1). ``_run`` reads
            # it off the Turn when it pops it.
            turn.cancel_settled_by = SETTLED_BY_BACKEND_REFRESH
            turn.cancel_defers_queue_resume = True
            if turn.task.done():
                self.in_flight.pop(session_id, None)
                from core.inbox_events import bus

                bus.publish(
                    "turn.end",
                    _turn_event_payload(session_id, turn.logical_turn_id),
                )
            else:
                turn.task.cancel()
                tasks_to_settle.append(turn.task)
            if backend in self._draining_backends:
                self._deferred_restart_sessions.setdefault(backend, set()).add(session_id)
            released_sessions.add(session_id)
            if not turn.logical_turn_id:
                legacy_projection_sessions.add(session_id)
        if tasks_to_settle:
            await asyncio.gather(*tasks_to_settle, return_exceptions=True)
        if self.controller is not None:
            for session_id in legacy_projection_sessions:
                self.controller.set_agent_status(session_id, "idle")
        released_restored: set[str] = set()
        for owner in restored_owners:
            owner_id = str(owner["id"])
            if owner["state"] == "starting":
                terminal = self._terminalize_durable_turn(
                    owner_id,
                    "failed",
                    settled_by=SETTLED_BY_BACKEND_REFRESH,
                    evidence_kind="backend_refresh_start_failed",
                    evidence={
                        "backend": backend,
                        "reason": "forced_refresh_during_unresolved_start",
                    },
                    abandon_unaccepted_start=True,
                )
                if terminal.get("changed"):
                    logger.error(
                        "Forced %s refresh failed unresolved durable Turn=%s",
                        backend,
                        owner_id,
                    )
            else:
                terminal = self._terminalize_durable_turn(
                    owner_id,
                    "canceled",
                    settled_by=SETTLED_BY_BACKEND_REFRESH,
                    evidence_kind="backend_refresh",
                )
            if terminal.get("changed"):
                released_restored.add(str(owner["session_id"]))
        for session_id in released_restored:
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

    def fail_restored_backend_turn(
        self,
        turn_id: str,
        *,
        backend: str,
        reason: str,
    ) -> bool:
        """Terminal-fail an exact durable owner whose backend restore cannot register."""

        terminal = self._terminalize_durable_turn(
            turn_id,
            "failed",
            settled_by=SETTLED_BY_BACKEND_REFRESH,
            evidence_kind="backend_restore_failed",
            evidence={"backend": backend, "reason": reason},
            abandon_unaccepted_start=True,
        )
        if terminal.get("changed"):
            logger.error(
                "Failed restored %s Turn=%s after backend registration error",
                backend,
                turn_id,
            )
        return bool(terminal.get("changed"))

    async def cancel(
        self,
        session_id: str,
        *,
        agent_run_id: str | None = None,
    ) -> dict:
        """Cancel a Session Turn or detach one exact Run from a shared Turn."""
        normalized_agent_run_id = (
            str(agent_run_id).strip() if agent_run_id is not None else None
        )
        if agent_run_id is not None and not normalized_agent_run_id:
            return {
                "ok": False,
                "code": "invalid_run_id",
                "session_id": session_id,
                "reason": "run_id_required",
            }
        turn = self.in_flight.get(session_id)
        if not self._durable_schema_available():
            if normalized_agent_run_id:
                return {
                    "ok": False,
                    "code": "atomic_run_cancel_unavailable",
                    "session_id": session_id,
                    "reason": "durable_turn_ownership_unavailable",
                }
            return await self._cancel_legacy_turn(session_id, turn)
        with self._sqlite_engine().connect() as conn:
            owner = delivery_store.active_turn(conn, session_id)
        if owner is None and not agent_run_id:
            return {
                "ok": False,
                "code": "not_in_flight",
                "session_id": session_id,
                "recovered_agent_status": self._repair_ownerless_running_projection(
                    session_id
                ),
            }
        memory_dead = turn is None or turn.task.done()
        if owner is not None and memory_dead and not agent_run_id:
            owner_id = str(owner["id"])
            restored_identity = self._active_identity(
                str(owner["backend"]),
                session_id,
                owner_id,
            )
            if restored_identity is None or restored_identity[0] != owner_id:
                start_receipt = str(owner.get("start_receipt_outcome") or "")
                starting = str(owner.get("state") or "") == "starting"
                never_started = starting and start_receipt not in {"accepted", "unknown"}
                unknown_start = starting and start_receipt == "unknown"
                if never_started:
                    with self._sqlite_engine().connect() as conn:
                        initial_delivery_ids = {
                            str(row["id"])
                            for row in delivery_store.initial_deliveries_for_turn(
                                conn,
                                owner_id,
                            )
                            if row["state"] == "claimed"
                        }
                    terminal = self._terminalize_durable_turn(
                        owner_id,
                        "not_written",
                        settled_by=SETTLED_BY_STOPPED,
                        evidence_kind="runtime_gone",
                        evidence={"reason": "stop_with_no_live_runtime"},
                        retire_unwritten_delivery_ids=initial_delivery_ids,
                        retire_unwritten_attempt_outcome="canceled",
                    )
                elif unknown_start:
                    terminal = self._terminalize_durable_turn(
                        owner_id,
                        "failed",
                        settled_by=SETTLED_BY_STOPPED,
                        evidence_kind="runtime_gone",
                        evidence={"reason": "stop_with_unknown_start"},
                        abandon_unaccepted_start=True,
                    )
                else:
                    terminal = self._terminalize_durable_turn(
                        owner_id,
                        "canceled",
                        settled_by=SETTLED_BY_STOPPED,
                        evidence_kind="runtime_gone",
                        evidence={"reason": "stop_with_no_live_runtime"},
                    )
                if terminal.get("changed"):
                    logger.info(
                        "Released durable Turn=%s for Session=%s after Stop found no live runtime",
                        owner_id,
                        session_id,
                    )
                    from core.inbox_events import bus

                    bus.publish(
                        "turn.end",
                        _turn_event_payload(session_id, owner_id),
                    )
                    successor_turn_id = str(terminal.get("successor_turn_id") or "")
                    if successor_turn_id:
                        await self._start_persisted_turn(successor_turn_id)
                    elif not terminal.get("defer_queue_resume"):
                        await self._resume_post_terminal(session_id)
                    return {
                        "ok": True,
                        "session_id": session_id,
                        "status": "stale_released",
                        "reason": "runtime_gone",
                    }
        result = await self.deliver(
            DeliveryRequest(
                session_id=session_id,
                priority="p0",
                content=None,
                expected_turn_id=(str(owner["id"]) if owner is not None else None),
                expected_exclusive_agent_run_id=(
                    normalized_agent_run_id
                ),
            ),
            context=turn.context if turn is not None else None,
        )
        if result.state == "run_detached":
            return {
                "ok": True,
                "session_id": session_id,
                "status": "run_detached",
                "reason": result.reason or "shared_turn",
            }
        if result.state in {"waiting_terminal", "interrupt_waiting"}:
            return {"ok": True, "session_id": session_id, "status": "cancel_requested"}
        if result.state == "settled":
            if normalized_agent_run_id:
                return {
                    "ok": True,
                    "session_id": session_id,
                    "status": "run_settled",
                    "reason": result.reason or "already_terminal",
                }
            return {
                "ok": True,
                "session_id": session_id,
                "status": "stale_released",
                "reason": result.reason or "already_terminal",
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
        turn.cancel_settled_by = SETTLED_BY_STOPPED
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
                    bus.publish(
                        "turn.end",
                        _turn_event_payload(session_id, turn.logical_turn_id),
                    )
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
            turn.cancel_settled_by = None
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

    async def send_now(
        self,
        session_id: str,
        *,
        expected_delivery_id: str | None = None,
    ) -> dict:
        """Promote the exact Delivery ordering head through empty P1."""
        with self._sqlite_engine().connect() as conn:
            head = delivery_store.ordering_head(conn, session_id)
        if head is None:
            return {"ok": True, "session_id": session_id, "status": "empty"}
        result = await self.deliver(
            DeliveryRequest(
                session_id=session_id,
                priority="p1",
                content=None,
                expected_delivery_id=expected_delivery_id,
            ),
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
        if not session_id:
            return False
        session_key = resolve_turn_sink_key(self.controller, context)
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
        """Map a release to the durable Turn outcome. The settlement wins.

        A named settlement in ``SETTLEMENTS_WITHOUT_RESULT`` says the Turn ended
        WITHOUT the backend producing a terminal result, so ``completed`` is never
        truthful for one -- yet only ``stopped`` used to be excluded, which left
        ``backend_refresh`` recording a retired runtime as a completed Turn. That is
        the same event ``release_for_backend_refresh`` writes as ``canceled``
        (``failed`` for a start it could not resolve), so the two paths for one
        service-initiated teardown disagreed depending on which reached the row
        first.

        ``canceled`` rather than ``failed`` because the Turn was retired, not broken;
        the RUN still settles ``failed`` with ``interrupt_reason=backend_refresh``
        through ``SETTLEMENT_TERMINAL_STATUS``, so invariant 2 of
        ``docs/plans/harness-run-reliability.md`` keeps its structured cause.
        ``restarted`` is deliberately absent -- its call site already forces
        ``failed`` before reaching here, and a service shutdown is not a cancellation.
        """

        non_completing = NON_COMPLETING_TURN_SETTLEMENTS.get(settled_by or "")
        if non_completing is not None:
            return non_completing
        if is_error:
            return "failed"
        return "completed"

    def _finish_durable_terminal_result(
        self,
        session_id: str,
        logical_turn_id: str,
        *,
        is_error: bool,
        settled_by: str | None,
        terminal_evidence: dict[str, Any] | None = None,
    ) -> None:
        try:
            terminal = self._terminalize_durable_turn(
                logical_turn_id,
                self._durable_terminal_outcome(
                    is_error=is_error,
                    settled_by=settled_by,
                ),
                settled_by=settled_by or SETTLED_BY_TERMINAL_RESULT,
                evidence_kind="terminal_result",
                evidence=terminal_evidence,
            )
        except Exception:
            logger.exception(
                "durable terminal reconciliation deferred after native completion for Turn=%s",
                logical_turn_id,
            )
            return
        current = self.in_flight.get(session_id)
        should_resume = bool(terminal.get("successor_turn_id")) or not bool(
            terminal.get("defer_queue_resume")
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

    def on_terminal_result(
        self,
        context: "MessageContext",
        *,
        is_error: bool,
        settled_by: str | None = None,
        terminal_evidence: dict[str, Any] | None = None,
    ) -> None:
        """OUTBOUND turn chokepoint for the active terminal ``result``.

        ``settled_by`` is the release's named settlement, latched alongside
        ``is_error`` so the post-delivery boundary can tell a Turn that ended
        WITHOUT a result on purpose from a backend that broke. Optional because a
        release may not name one; absent, the flag decides as before.
        """
        if self.controller is None:
            return
        if not self.is_active_emit(context):
            return
        # The dispatcher calls this before delivery. Latch evidence only; the
        # controller's post-delivery boundary performs the durable transition.
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
            try:
                sink = self.get_turn_sink(resolve_turn_sink_key(self.controller, context))
            except Exception:
                logger.debug("failed to inspect terminal Turn sink", exc_info=True)
            if isinstance(sink, dict):
                sink["terminal_evidence"] = dict(terminal_evidence or {})
        payload = dict(getattr(context, "platform_specific", None) or {})
        payload[_TERMINAL_RESULT_LATCH_KEY] = {
            "session_id": session_id,
            "logical_turn_id": logical_turn_id,
            "is_error": is_error,
            "settled_by": settled_by or None,
            "terminal_evidence": dict(terminal_evidence or {}),
        }
        context.platform_specific = payload

    def on_terminal_delivery_complete(self, context: "MessageContext") -> None:
        """Commit terminal evidence after the output delivery attempt has settled."""

        if not self._pop_context_flag(context, _SHOW_CHECKPOINT_TERMINAL_PENDING_KEY):
            return
        self._end_show_checkpoint(context)
        payload = dict(getattr(context, "platform_specific", None) or {})
        latch = payload.pop(_TERMINAL_RESULT_LATCH_KEY, None)
        context.platform_specific = payload
        if not isinstance(latch, dict):
            return
        session_id = str(latch.get("session_id") or "")
        logical_turn_id = str(latch.get("logical_turn_id") or "")
        is_error = bool(latch.get("is_error"))
        latched_settlement = str(latch.get("settled_by") or "")
        if not session_id:
            return
        durable_turn_exists = False
        if logical_turn_id and self._durable_schema_available():
            try:
                with self._sqlite_engine().connect() as conn:
                    durable_turn_exists = (
                        delivery_store.get_turn(conn, logical_turn_id) is not None
                    )
            except Exception:
                logger.debug(
                    "failed to inspect delivered terminal Turn authority",
                    exc_info=True,
                )
        if not durable_turn_exists:
            # No durable Turn row owns this session's outcome (IM, CLI, legacy),
            # so this projection IS the session's terminal state. ``is_error``
            # alone would call every result-less release a failure -- including a
            # release whose settlement says the Turn was ended on purpose. A
            # service-initiated backend teardown is the case that matters: the
            # flag is honestly ``True`` (nothing answered) while the settlement
            # says infrastructure, not fault, and the sidebar has no third dot to
            # say so. ``idle`` is what the stop path already projects for the
            # other member of that map, so one deliberate non-completion no
            # longer reads two different ways depending on which one it was.
            self.controller.set_agent_status(
                session_id,
                (
                    "failed"
                    if is_error and latched_settlement not in NON_COMPLETING_TURN_SETTLEMENTS
                    else "idle"
                ),
            )
            return
        current = self.in_flight.get(session_id)
        sink = None
        try:
            sink = self.get_turn_sink(resolve_turn_sink_key(self.controller, context))
        except Exception:
            logger.debug("failed to inspect delivered terminal Turn sink", exc_info=True)
        self._finish_durable_terminal_result(
            session_id,
            logical_turn_id,
            is_error=is_error,
            settled_by=(
                str((sink or {}).get("settled_by") or "")
                or (current.cancel_settled_by if current is not None else None)
            ),
            terminal_evidence=(
                dict(latch.get("terminal_evidence") or {})
                if isinstance(latch.get("terminal_evidence"), dict)
                else None
            ),
        )

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
        session_key = resolve_turn_sink_key(self.controller, context)
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
            backend = self._context_backend(context)
            with self._runtime_start_owner(session_id, backend) as start_owner, engine.begin() as conn:
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
                    language = self._controller_language()
                    trigger_text = str(
                        payload.get("agent_initiated_trigger_text")
                        or i18n_t("harness.agentInitiatedContinuation", language)
                    )
                    delivery = delivery_store.insert_delivery(
                        conn,
                        delivery_id=delivery_id,
                        session_id=session_id,
                        priority="p3",
                        state="reserved",
                        snapshot=delivery_store.message_snapshot(
                            scope_id=session_row["scope_id"],
                            session_id=session_id,
                            platform="avibe",
                            author="harness",
                            source="harness",
                            # This durable row owns the backend-started Turn for
                            # Stop/FSM/history, but it is not a user instruction
                            # and must not render as a chat bubble.
                            message_type="agent_initiated",
                            text=trigger_text,
                            metadata={
                                "source": "agent_initiated",
                                "runtime_key": payload.get("agent_runtime_turn_key"),
                            },
                        ),
                        dispatch_text="",
                        history_event={
                            "kind": "admission",
                            "turn_id": turn_token,
                            "outcome": "backend_initiated",
                        },
                    )
                    claimed = self._claim_start_batch(
                        conn,
                        owner=start_owner,
                        turn_id=turn_token,
                        session_id=session_id,
                        backend=backend,
                        deliveries=[delivery],
                        dispatch_text="",
                    )
                    if claimed is None:
                        return False
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
                    materialized = delivery_store.materialize_start_acceptance(
                        conn,
                        turn_id=turn_token,
                        evidence={"kind": "backend_initiated_output"},
                    )
                    if not materialized:
                        raise RuntimeError(
                            "agent-initiated Turn could not materialize its trigger Delivery"
                        )
                    materialized_delivery_id = str(
                        materialized[0].get("message_id") or materialized[0]["id"]
                    )
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
            durable_terminal_result: dict[str, Any] = {}
            try:
                await done.wait()
            except asyncio.CancelledError:
                cancelled = True
                raise
            finally:
                sink = self.get_turn_sink(session_key)
                settled_by = str((sink or {}).get("settled_by") or "")
                current = self.in_flight.get(session_id)
                turn = (
                    current
                    if current is not None
                    and current.task is asyncio.current_task()
                    else None
                )
                effective_settled_by = (
                    (turn.cancel_settled_by if turn is not None else None)
                    or SETTLED_BY_STOPPED
                    if cancelled
                    else settled_by
                )
                terminal_evidence = (sink or {}).get("terminal_evidence")
                self.pop_turn_sink(session_key, done)
                terminal_is_error = bool(
                    turn is not None and turn.terminal_is_error
                )
                cancel_defers_queue_resume = bool(
                    turn is not None and turn.cancel_defers_queue_resume
                )
                queue_resume_deferred = bool(
                    cancel_defers_queue_resume
                    or effective_settled_by == SETTLED_BY_RESTARTED
                )
                if turn is not None:
                    self.in_flight.pop(session_id, None)
                if turn is not None:
                    bus.publish(
                        "turn.end",
                        _turn_event_payload(session_id, turn_token),
                    )
                if durable_turn_registered:
                    try:
                        durable_terminal_result = self._terminalize_durable_turn(
                            turn_token,
                            (
                                "failed"
                                if effective_settled_by == SETTLED_BY_RESTARTED
                                else self._durable_terminal_outcome(
                                    is_error=terminal_is_error,
                                    settled_by=effective_settled_by or None,
                                )
                            ),
                            settled_by=(
                                effective_settled_by or SETTLED_BY_TERMINAL_RESULT
                            ),
                            evidence_kind=(
                                "service_shutdown"
                                if effective_settled_by == SETTLED_BY_RESTARTED
                                else "agent_initiated_terminal"
                            ),
                            evidence=(
                                dict(terminal_evidence)
                                if isinstance(terminal_evidence, dict)
                                else None
                            ),
                            resume_successors=not queue_resume_deferred,
                        )
                        if queue_resume_deferred:
                            durable_terminal_result["defer_queue_resume"] = True
                    except Exception:
                        logger.exception(
                            "agent-initiated durable terminal reconciliation deferred "
                            "for Turn=%s",
                            turn_token,
                        )
                        durable_terminal_result = {"defer_queue_resume": True}
                should_flush = not (
                    queue_resume_deferred
                    or bool(durable_terminal_result.get("defer_queue_resume"))
                )
                if should_flush:
                    try:
                        if durable_turn_registered:
                            await self._resume_post_terminal(session_id)
                        else:
                            await self.flush_queue(session_id)
                    except Exception:
                        logger.debug("agent-initiated turn: queue resume failed", exc_info=True)
                elif (
                    durable_turn_registered
                    and effective_settled_by != SETTLED_BY_RESTARTED
                ):
                    await self._resume_linked_control_successor(
                        session_id,
                        turn_token,
                    )

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
        bus.publish(
            "turn.start",
            _turn_event_payload(session_id, turn_token),
        )
        if self._pop_context_flag(context, _SHOW_CHECKPOINT_DEFERRED_START_KEY):
            self._begin_show_checkpoint(context)
        return True

    def is_active_emit(self, context: "MessageContext") -> bool:
        """Whether an emit belongs to the live turn (not a superseded one). Fail-open
        when there's no sink registry / no live sink (non-streaming turns still
        settle), else apply the one token rule. Centralizes the old
        ``ConsolidatedMessageDispatcher._is_active_turn``."""
        get_sink = getattr(self.controller, "get_turn_sink", None)
        if not callable(get_sink):
            return True
        try:
            sink = get_sink(resolve_turn_sink_key(self.controller, context))
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
        accepted_run_ids = spec.get("accepted_agent_run_ids")
        if isinstance(accepted_run_ids, list):
            identity["accepted_agent_run_ids"] = list(accepted_run_ids)
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
        try:
            session_key = resolve_turn_sink_key(self.controller, context)
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
        attribution_keys = (
            "task_trigger_kind",
            "task_execution_id",
            "accepted_agent_run_ids",
        )
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
            elif isinstance(value, list):
                value = list(value)
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
    def reset_legacy_ownerless_status() -> None:
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
        """Restore the status projection for pre-durable OpenCode polls only.

        Durable owners are reconciled once, after every backend identity has been
        restored.  This callback runs while OpenCode is still rebuilding its maps,
        so it must never start durable reconciliation itself.
        """
        if not session_id or self.controller is None:
            return
        if not self._durable_schema_available():
            self.controller.set_agent_status(session_id, "running")
            return
        try:
            with self._sqlite_engine().connect() as conn:
                has_history = session_id in delivery_store.session_ids_with_turn_history(conn)
        except Exception:
            logger.debug(
                "could not inspect durable Turn history for restored poll %s",
                session_id,
                exc_info=True,
            )
            return
        if not has_history:
            self.controller.set_agent_status(session_id, "running")
