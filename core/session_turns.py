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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Optional

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from core.web_push_notifications import WEB_PUSH_USER_KEY_METADATA, WEB_PUSH_USER_KEYS_METADATA
from core.services.dispatch import SOURCE_HUMAN, SOURCE_SCHEDULED, dispatch_turn
from storage import messages_service
from storage.db import get_cached_sqlite_engine
from storage.background import normalize_run_status
from storage.models import agent_runs, messages
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


def _run_metadata_holds_workbench_queue(value: Any) -> bool:
    try:
        metadata = json.loads(value or "{}")
    except (TypeError, ValueError):
        return False
    return bool(
        isinstance(metadata, dict)
        and metadata.get("workbench_queue_holds_run") is True
    )


def _scheduled_provenance(row: dict) -> Optional[dict]:
    metadata = row.get("metadata") or {}
    provenance = metadata.get(SCHEDULED_PROVENANCE_KEY)
    return provenance if isinstance(provenance, dict) else None


def _agent_run_merge_definition_id(spec: dict) -> str:
    """Return the coalescing bucket for direct Agent Run queue rows.

    Callback-backed runs can deliver results into different caller sessions, so
    keep those rows isolated by execution id. Plain CLI/direct runs may coalesce,
    and their prompt builder always includes every queued message verbatim.
    """
    execution_id = str(spec.get("task_execution_id") or "").strip()
    if "source_kind" not in spec and "callback_session_id" not in spec:
        return f"agent_run:{execution_id}" if execution_id else ""
    if spec.get("callback_session_id") or spec.get("source_kind") == "callback":
        return f"agent_run:{execution_id}" if execution_id else ""
    return "agent_run"


def _scheduled_merge_key(row: dict) -> Optional[tuple[str, ...]]:
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
    delivery_override = spec.get("delivery_override") if isinstance(spec.get("delivery_override"), dict) else {}
    delivery_alias = spec.get("scheduled_delivery_alias") if isinstance(spec.get("scheduled_delivery_alias"), dict) else {}
    return (
        trigger_kind,
        definition_id,
        str(spec.get("agent_session_id") or ""),
        str(spec.get("vibe_agent_name") or ""),
        str(spec.get(SCHEDULED_TARGET_AGENT_KEY) or ""),
        str((spec.get("agent_session_target") or {}).get("agent_name") or "")
        if isinstance(spec.get("agent_session_target"), dict)
        else "",
        str(spec.get("callback_session_id") or ""),
        str(spec.get("source_kind") or ""),
        str(spec.get("source_actor") or ""),
        str(spec.get("parent_run_id") or ""),
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


def _within_scheduled_merge_window(previous: dict, current: dict) -> bool:
    prev_ts = _parse_queue_timestamp(previous.get("created_at"))
    current_ts = _parse_queue_timestamp(current.get("created_at"))
    if prev_ts is None or current_ts is None:
        return False
    delta = (current_ts - prev_ts).total_seconds()
    return 0 <= delta <= SCHEDULED_QUEUE_MERGE_WINDOW_SECONDS


def _collect_scheduled_segment(rows: list[dict]) -> list[dict]:
    if not rows:
        return []
    first_key = _scheduled_merge_key(rows[0])
    if first_key is None:
        return [rows[0]]
    segment = [rows[0]]
    latest = rows[0]
    for row in rows[1:]:
        if _scheduled_merge_key(row) != first_key:
            break
        if not _within_scheduled_merge_window(latest, row):
            break
        segment.append(row)
        latest = row
    return segment


def _build_scheduled_segment_text(segment: list[dict]) -> str:
    texts = [str(row.get("text") or "") for row in segment if str(row.get("text") or "")]
    if not texts:
        return ""
    if len(texts) == 1:
        return texts[0]

    if _scheduled_segment_trigger_kind(segment) == "agent_run":
        return "\n\n---\n\n".join(texts)

    lang = _scheduled_segment_language(segment)
    parts = [
        texts[0],
        "\n\n"
        + i18n_t(
            "harness.queueCoalesced.summary",
            lang,
            count=len(texts),
            window=SCHEDULED_QUEUE_MERGE_WINDOW_SECONDS,
        ),
    ]
    if len(texts) <= SCHEDULED_QUEUE_FULL_DETAIL_LIMIT:
        parts.append(
            "\n\n"
            + i18n_t("harness.queueCoalesced.messagesLabel", lang)
            + "\n"
            + "\n\n---\n\n".join(texts[1:])
        )
    else:
        parts.append(
            "\n\n"
            + i18n_t("harness.queueCoalesced.latestLabel", lang, additional=len(texts) - 1)
            + f"\n{texts[-1]}"
        )
    if len(texts) >= SCHEDULED_QUEUE_BURST_HINT_THRESHOLD:
        parts.append("\n\n" + i18n_t("harness.queueCoalesced.burstHint", lang))
    return "".join(parts)


def _scheduled_segment_trigger_kind(segment: list[dict]) -> str:
    spec = (_scheduled_provenance(segment[0]) or {}).get("platform_specific") or {} if segment else {}
    return str(spec.get("task_trigger_kind") or "").strip() if isinstance(spec, dict) else ""


def _scheduled_segment_language(segment: list[dict]) -> str:
    for row in segment:
        spec = (_scheduled_provenance(row) or {}).get("platform_specific") or {}
        if not isinstance(spec, dict):
            continue
        lang = str(spec.get("language") or spec.get("lang") or "").strip()
        if lang:
            return lang
    return "en"


def _scheduled_segment_native_ids(segment: list[dict]) -> list[str]:
    native_ids: list[str] = []
    for row in segment:
        native_id = str(row.get("native_message_id") or "").strip()
        if not native_id:
            native_id = str((_scheduled_provenance(row) or {}).get("message_id") or "").strip()
        if native_id and native_id not in native_ids:
            native_ids.append(native_id)
    return native_ids


def _scheduled_segment_execution_id(row: dict) -> str:
    spec = (_scheduled_provenance(row) or {}).get("platform_specific") or {}
    if not isinstance(spec, dict):
        return ""
    return str(spec.get("task_execution_id") or "").strip()


def _scheduled_row_execution_ids(row: dict) -> list[str]:
    execution_ids: list[str] = []
    execution_id = _scheduled_segment_execution_id(row)
    if execution_id:
        execution_ids.append(execution_id)
    spec = (_scheduled_provenance(row) or {}).get("platform_specific") or {}
    coalesced = spec.get("coalesced_queue") if isinstance(spec, dict) else None
    coalesced_ids = coalesced.get("execution_ids") if isinstance(coalesced, dict) else None
    if isinstance(coalesced_ids, list):
        for value in coalesced_ids:
            coalesced_id = str(value or "").strip()
            if coalesced_id and coalesced_id not in execution_ids:
                execution_ids.append(coalesced_id)
    return execution_ids


def _scheduled_segment_execution_ids(segment: list[dict]) -> list[str]:
    execution_ids: list[str] = []
    for row in segment:
        for execution_id in _scheduled_row_execution_ids(row):
            if execution_id not in execution_ids:
                execution_ids.append(execution_id)
    return execution_ids


def _scheduled_segment_rows_for_execution_ids(segment: list[dict], execution_ids: set[str]) -> list[dict]:
    return [row for row in segment if set(_scheduled_row_execution_ids(row)) & execution_ids]


def _scheduled_segment_stale_row_ids(segment: list[dict], queued_ids: set[str]) -> list[str]:
    row_ids: list[str] = []
    for row in segment:
        row_id = row.get("id")
        if not row_id:
            continue
        represented = set(_scheduled_row_execution_ids(row))
        if represented and not (represented & queued_ids):
            row_ids.append(row_id)
    return row_ids


def _filter_coalesced_agent_run_provenance(provenance: dict, execution_ids: list[str]) -> dict:
    if not execution_ids:
        return provenance
    execution_set = set(execution_ids)
    coalesced = provenance.get("coalesced_queue")
    if not isinstance(coalesced, dict):
        return provenance
    filtered = dict(coalesced)
    raw_ids = coalesced.get("execution_ids")
    if isinstance(raw_ids, list):
        filtered["execution_ids"] = [
            str(value)
            for value in raw_ids
            if str(value or "").strip() in execution_set
        ]
    raw_messages = coalesced.get("messages")
    if isinstance(raw_messages, list):
        messages = [
            item
            for item in raw_messages
            if isinstance(item, dict)
            and str(item.get("execution_id") or "").strip() in execution_set
        ]
        filtered["messages"] = messages
        prompt_parts = [
            str(item.get("message") or item.get("prompt") or "")
            for item in messages
            if str(item.get("message") or item.get("prompt") or "")
        ]
        if prompt_parts:
            filtered["prompt"] = "\n\n---\n\n".join(prompt_parts)
        else:
            filtered.pop("prompt", None)
    result = dict(provenance)
    result["coalesced_queue"] = filtered
    current_id = str(result.get("task_execution_id") or "").strip()
    if current_id not in execution_set:
        result["task_execution_id"] = execution_ids[0]
    return result


def _scheduled_segment_suppresses_delivery(segment: list[dict]) -> bool:
    for row in segment:
        spec = (_scheduled_provenance(row) or {}).get("platform_specific") or {}
        if isinstance(spec, dict) and bool(spec.get("suppress_delivery")):
            return True
    return False


def _write_coalesced_native_id_markers(conn: Any, segment: list[dict]) -> None:
    """Keep native-id dedupe coverage for coalesced queued harness rows.

    The first native id is restored on the dispatched context and mirrored as
    the visible harness prompt. Later coalesced queued rows are deleted before
    dispatch, so write hidden rows for their native ids; otherwise a waiter retry
    for one of those callbacks would not hit ``uq_messages_platform_native``.
    """
    native_ids = _scheduled_segment_native_ids(segment)
    if not native_ids:
        return
    first = segment[0]
    suppresses_delivery = _scheduled_segment_suppresses_delivery(segment)
    marker_ids = native_ids if suppresses_delivery else native_ids[1:]
    if not marker_ids:
        return

    for native_id in marker_ids:
        try:
            messages_service.append(
                conn,
                scope_id=first["scope_id"],
                session_id=first.get("session_id"),
                platform=first.get("platform") or "avibe",
                author="harness",
                source="harness",
                message_type=messages_service.HARNESS_DEDUPE_TYPE,
                text="",
                metadata={"coalesced_from": first.get("id"), "suppressed_delivery": suppresses_delivery},
                native_message_id=native_id,
            )
        except IntegrityError:
            logger.debug("queue flush: coalesced native id marker already exists for %s", native_id, exc_info=True)


def _restore_queued_rows(conn: Any, rows: list[dict]) -> None:
    native_ids = [
        str(row.get("native_message_id") or "").strip()
        for row in rows
        if str(row.get("native_message_id") or "").strip()
    ]
    if native_ids:
        conn.execute(
            messages.delete()
            .where(messages.c.type == messages_service.HARNESS_DEDUPE_TYPE)
            .where(messages.c.native_message_id.in_(native_ids))
        )
    for row in rows:
        payload = {
            "id": row["id"],
            "scope_id": row["scope_id"],
            "session_id": row.get("session_id"),
            "platform": row.get("platform") or "avibe",
            "author": row.get("author") or "harness",
            "type": messages_service.QUEUED_TYPE,
            "author_id": row.get("author_id"),
            "author_name": row.get("author_name"),
            "source": row.get("source"),
            "native_message_id": row.get("native_message_id"),
            "parent_native_message_id": row.get("parent_native_message_id"),
            "content_text": row.get("text") or "",
            "content_json": json.dumps(row.get("content") or {"text": row.get("text") or ""}),
            "metadata_json": json.dumps(row.get("metadata") or {}),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at") or row.get("created_at"),
            "delivered_at": row.get("delivered_at"),
            "read_at": row.get("read_at"),
        }
        try:
            conn.execute(messages.insert().values(**payload))
        except IntegrityError:
            logger.debug("queue flush: queued row already restored or replaced for %s", row.get("id"), exc_info=True)


def _claim_agent_run_segment_and_retire_queue(
    conn: Any,
    *,
    run_ids: list[str],
    segment: list[dict],
) -> list[str]:
    from storage.background import claim_queued_runs_for_workbench_in_connection

    claimed_run_ids = claim_queued_runs_for_workbench_in_connection(conn, run_ids)
    if set(claimed_run_ids) != set(run_ids):
        return claimed_run_ids
    messages_service.delete_queued(conn, [r["id"] for r in segment if r.get("id")])
    _write_coalesced_native_id_markers(conn, segment)
    return claimed_run_ids


def _scheduled_claimed_queue_row_ids(conn: Any, segment: list[dict]) -> set[str]:
    native_ids = _scheduled_segment_native_ids(segment)
    if not native_ids:
        return set()
    native_by_row_id = {
        str(row.get("id") or ""): str(row.get("native_message_id") or (_scheduled_provenance(row) or {}).get("message_id") or "").strip()
        for row in segment
        if row.get("id")
    }
    native_to_row_ids: dict[str, set[str]] = {}
    for row_id, native_id in native_by_row_id.items():
        if native_id:
            native_to_row_ids.setdefault(native_id, set()).add(row_id)
    platform = str(segment[0].get("platform") or "avibe")
    rows = (
        conn.execute(
            select(messages.c.id, messages.c.native_message_id)
            .where(messages.c.platform == platform)
            .where(messages.c.native_message_id.in_(native_ids))
        )
        .all()
    )
    claimed_row_ids: set[str] = set()
    segment_row_ids = set(native_by_row_id)
    for row_id, native_id in rows:
        if str(row_id) in segment_row_ids:
            continue
        claimed_row_ids.update(native_to_row_ids.get(str(native_id or ""), set()))
    return claimed_row_ids


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

    def is_in_flight(self, session_id: Optional[str]) -> bool:
        """True when ``session_id`` has an active (RUNNING) turn."""
        return bool(session_id) and session_id in self.in_flight

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
                await self.flush_queue(session_id)

    def active_session_ids_for_backend(self, backend: str) -> set[str]:
        return {
            session_id
            for session_id, turn in self.in_flight.items()
            if not turn.task.done() and self._context_backend(turn.context) == backend
        }

    def active_runtime_session_ids_for_backend(self, backend: str) -> set[str]:
        """Active Sessions that actually entered the old backend generation."""
        return {
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

    @staticmethod
    async def _noop_chunk(_envelope: dict) -> None:
        # Chunks are discarded — the browser renders from ``message.new``.
        return None

    async def submit(
        self,
        session_id: Optional[str],
        context: "MessageContext",
        text: str,
        *,
        source: str = SOURCE_HUMAN,
        enqueue: Optional[Callable[[], None]] = None,
    ) -> str:
        """Unified turn entry for BOTH Chat and the scheduler: idle → run now; busy
        (or a pre-existing send-while-busy queue) → enqueue and run later.

        Returns ``"ran"`` or ``"enqueued"``. The busy / pre-existing-queue decision,
        the idle-with-queue drain, and the run are unified here; the caller supplies
        ``enqueue`` — a 0-arg callable that persists the SOURCE-specific queued row
        (Chat promotes its pre-saved pending row; the scheduler appends a harness
        row) — because that row's shape depends on the request. The in_flight check
        and the enqueue have no ``await`` between them (single-threaded loop), so a
        running turn cannot end + flush in the gap — the enqueue stays atomic.
        """
        if not (isinstance(session_id, str) and session_id):
            # No session key (CLI-style) — just run; nothing to queue against.
            await self._run(None, context, text, source=source)
            return "ran"

        backend = self._context_backend(context)
        entry = self.in_flight.get(session_id)
        busy = entry is not None and not entry.task.done()
        # Enqueue when a turn is running OR a prior Stop left queued rows behind — the
        # new message must run AFTER them, not jump ahead (Codex P2).
        if backend in self._draining_backends:
            should_enqueue = True
            self._deferred_restart_sessions.setdefault(backend, set()).add(session_id)
        elif busy:
            should_enqueue = True
        else:
            with self._sqlite_engine().connect() as conn:
                should_enqueue = bool(messages_service.list_queued(conn, session_id))
        if should_enqueue:
            if enqueue is not None:
                enqueue()
            if busy or backend in self._draining_backends:
                # The row joins the active turn's queue and stays until it drains —
                # surface the queue growth NOW so the UI reflects it immediately
                # (the later flush emits its own queue.updated when it pops). This
                # closes the enqueue-time gap for BOTH Chat and scheduled sends.
                from core.inbox_events import bus

                bus.publish("queue.updated", {"session_id": session_id})
            else:
                # Idle + pre-existing queue → no running turn to flush behind, so
                # drain the whole queue (this row included) now, in order. flush_queue
                # publishes queue.updated itself.
                await self.flush_queue(session_id)
            return "enqueued"
        await self._run(session_id, context, text, source=source)
        return "ran"

    async def _run(
        self,
        session_id: Optional[str],
        context: "MessageContext",
        text: str,
        *,
        source: str = SOURCE_HUMAN,
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

        async def _runner() -> None:
            cancelled = False
            failed = False
            try:
                await dispatch_turn(
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
            except asyncio.CancelledError:
                cancelled = True
                raise
            except Exception:
                # dispatch_turn raised before any backend turn was actually
                # dispatched (missing/disabled backend, synchronous setup error).
                # No agent reply was produced, so this is a terminal FAILURE — it must
                # NOT auto-flush the send-while-busy queue onto a fresh turn (Codex
                # P2). (An explicit send-now flush_on_cancel still flushes.)
                failed = True
                logger.exception("internal async dispatch failed for session=%s", session_id)
            finally:
                if isinstance(session_id, str):
                    # The turn is over — the agent emitted its terminal result, the
                    # user stopped it, or dispatch raised before any backend turn.
                    # NO turn-duration timeout: the slot is freed only by a real
                    # terminal signal here (Phase 1a — STUCK/sentinel removed).
                    turn = self.in_flight.pop(session_id, None)
                    if turn is not None:
                        bus.publish("turn.end", {"session_id": session_id})
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
                    # Flush intents ride on the popped Turn (set by cancel / send_now),
                    # so they retire with it — no parallel set to discard. Don't flush
                    # after a plain Stop (keep the queue) or a terminal failure; send-now
                    # still forces a flush via flush_on_cancel.
                    should_flush = (
                        (not cancelled and not failed and not (turn is not None and turn.stop_no_flush))
                        or (turn is not None and turn.flush_on_cancel)
                    )
                    backend = self._context_backend(context)
                    if should_flush and backend in self._draining_backends:
                        self._deferred_restart_sessions.setdefault(backend, set()).add(session_id)
                    elif should_flush:
                        await self.flush_queue(session_id)

        task = asyncio.create_task(_runner(), name="internal-dispatch-async")
        if isinstance(session_id, str) and session_id:
            self.in_flight[session_id] = Turn(
                task=task,
                context=context,
                started_at=_utc_now_iso(),
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
        """Drain the send-while-busy queue ONE segment per call — the turn's
        completion re-flushes the next, so segments run in order, one at a time.

        A leading run of consecutive USER rows is merged into a single user turn (the
        user's choice — one dispatch, not N). A SCHEDULED row (it carries stored
        provenance) is NOT merged: it runs as its OWN ``SOURCE_SCHEDULED`` turn with
        its delivery / attribution provenance restored, so a scheduled run that was
        enqueued behind an active turn keeps its suppress-delivery / delivery-target /
        source when it finally runs (#84). Returns True if a turn was started, False
        on an empty queue / failure."""
        from core.inbox_events import bus
        from core.workbench_media import file_attachments_from_specs, resolve_attachment_specs
        from storage.background import run_update_event_transaction

        if not session_id:
            return False
        if self._build_context is None:
            logger.error("queue flush: no build_context bound for session=%s", session_id)
            return False
        try:
            context = await asyncio.to_thread(self._build_context, session_id)
        except Exception:
            logger.exception("queue flush: failed to build context for session=%s", session_id)
            return False
        backend = self._context_backend(context)
        if backend in self._draining_backends:
            self._deferred_restart_sessions.setdefault(backend, set()).add(session_id)
            return False

        is_scheduled = False
        scheduled_text = ""
        scheduled_prov: dict = {}
        scheduled_message_id = None
        scheduled_native_ids: list[str] = []
        dropped_duplicate_segment = False
        user_row = None
        inbox_row = None
        attachment_specs: list = []
        pending_agent_run_ids: list[str] = []
        pending_scheduled_segment: list[dict] = []
        claimed_agent_run_ids: list[str] = []
        engine = self._sqlite_engine()
        try:
            with run_update_event_transaction(engine) as conn:
                rows = messages_service.list_queued(conn, session_id)
                if not rows:
                    return False
                if _scheduled_provenance(rows[0]) is not None:
                    from storage.background import inspect_queued_runs_for_workbench_in_connection

                    # Scheduled segment: a leading run of same-definition harness
                    # rows within the rolling merge window runs as one scheduled
                    # turn. Rows remain visible individually while queued; only the
                    # Agent-facing dispatch is coalesced.
                    is_scheduled = True
                    initial_segment = _collect_scheduled_segment(rows)
                    claimed_row_ids = _scheduled_claimed_queue_row_ids(conn, initial_segment)
                    if claimed_row_ids:
                        messages_service.delete_queued(conn, list(claimed_row_ids))
                        bus.publish("queue.updated", {"session_id": session_id})
                        segment = [r for r in initial_segment if str(r.get("id") or "") not in claimed_row_ids]
                        if not segment:
                            dropped_duplicate_segment = True
                    else:
                        segment = initial_segment
                    if dropped_duplicate_segment:
                        pass
                    else:
                        if _scheduled_segment_trigger_kind(segment) == "agent_run":
                            run_ids = _scheduled_segment_execution_ids(segment)
                            queued_run_ids, stale_run_ids = inspect_queued_runs_for_workbench_in_connection(conn, run_ids)
                            if stale_run_ids:
                                stale_set = set(stale_run_ids)
                                queued_set = set(queued_run_ids)
                                stale_row_ids = _scheduled_segment_stale_row_ids(segment, queued_set)
                                if stale_row_ids:
                                    messages_service.delete_queued(conn, stale_row_ids)
                                    bus.publish("queue.updated", {"session_id": session_id})
                                logger.info(
                                    "queue flush: removed stale coalesced agent_run rows before dispatching survivors: %s",
                                    ",".join(sorted(stale_set)),
                                )
                                segment = _scheduled_segment_rows_for_execution_ids(segment, queued_set)
                                if not segment:
                                    dropped_duplicate_segment = True

                    if dropped_duplicate_segment:
                        pass
                    else:
                        scheduled_text = _build_scheduled_segment_text(segment)
                        scheduled_native_ids = _scheduled_segment_native_ids(segment)
                        prov = _scheduled_provenance(segment[0]) or {}
                        scheduled_message_id = scheduled_native_ids[0] if scheduled_native_ids else None
                        scheduled_prov = prov.get("platform_specific") or {}
                        if len(segment) > 1:
                            scheduled_prov = dict(scheduled_prov)
                            scheduled_prov["coalesced_queue"] = {
                                "count": len(segment),
                                "window_seconds": SCHEDULED_QUEUE_MERGE_WINDOW_SECONDS,
                                "message_ids": [r.get("id") for r in segment if r.get("id")],
                                "native_message_ids": scheduled_native_ids,
                                "execution_ids": queued_run_ids
                                if _scheduled_segment_trigger_kind(segment) == "agent_run"
                                else _scheduled_segment_execution_ids(segment),
                            }
                        if scheduled_prov.get("task_trigger_kind") == "agent_run" and queued_run_ids:
                            pending_agent_run_ids = list(queued_run_ids)
                            scheduled_prov = _filter_coalesced_agent_run_provenance(
                                scheduled_prov,
                                pending_agent_run_ids,
                            )
                            scheduled_message_id = f"agent_run:{pending_agent_run_ids[0]}"
                            coalesced = scheduled_prov.get("coalesced_queue")
                            if isinstance(coalesced, dict):
                                coalesced_prompt = str(coalesced.get("prompt") or "")
                                if coalesced_prompt:
                                    scheduled_text = coalesced_prompt
                            pending_scheduled_segment = segment
                else:
                    # User segment: the leading run of consecutive non-scheduled rows
                    # (stop at the first scheduled row so it stays its own turn).
                    segment = []
                    for r in rows:
                        if _scheduled_provenance(r) is not None:
                            break
                        segment.append(r)
                if segment and not pending_agent_run_ids:
                    messages_service.delete_queued(conn, [r["id"] for r in segment])
                if not is_scheduled:
                    texts = [r.get("text") for r in segment if (r.get("text") or "").strip()]
                    # Carry attachments queued in this user segment so a file
                    # attached while the agent was busy still reaches the merged
                    # turn. An attachment-ONLY segment has empty texts but must
                    # still run (the agent reads the files), so guard on both.
                    queued_attachments = [
                        att
                        for r in segment
                        for att in ((r.get("content") or {}).get("attachments") or [])
                    ]
                    if not texts and not queued_attachments:
                        return False
                    user_owners = list(
                        dict.fromkeys(
                            owner.strip()
                            for r in segment
                            if isinstance((owner := (r.get("metadata") or {}).get(WEB_PUSH_USER_KEY_METADATA)), str)
                            and owner.strip()
                        )
                    )
                    user_owner = user_owners[0] if len(user_owners) == 1 else None
                    user_metadata = None
                    if user_owner:
                        user_metadata = {WEB_PUSH_USER_KEY_METADATA: user_owner}
                    elif user_owners:
                        user_metadata = {WEB_PUSH_USER_KEYS_METADATA: user_owners}
                    attachment_specs = resolve_attachment_specs(
                        conn, session_id=session_id, attachments=queued_attachments
                    )
                    user_row = messages_service.append(
                        conn,
                        scope_id=segment[0]["scope_id"],
                        session_id=session_id,
                        platform="avibe",
                        author="user",
                        source="user",
                        message_type="user",
                        text="\n".join(texts),
                        content={"attachments": queued_attachments} if queued_attachments else None,
                        metadata=user_metadata,
                        author_id=user_owner,
                    )
                    inbox_row = messages_service.get_inbox_session(conn, session_id)
        except Exception:
            logger.exception("queue flush: failed to claim/merge for session=%s", session_id)
            return False

        if dropped_duplicate_segment:
            return await self.flush_queue(session_id)

        # Surface the flushed (merged) user message + bump the inbox card so other
        # workbench views re-rank / flip 'replied' without waiting for the result
        # (Codex P2). A scheduled segment has NO user row — its prompt is mirrored by
        # its own dispatch, exactly as a non-enqueued scheduled run. Either way the
        # queue changed.
        if user_row is not None:
            bus.publish("message.new", user_row)
            if inbox_row is not None:
                bus.publish("inbox.session.updated", inbox_row)
        bus.publish("queue.updated", {"session_id": session_id})

        if not is_scheduled:
            # Carry the queued segment's uploaded files into the merged turn.
            context.files = file_attachments_from_specs(attachment_specs)
            await self._run(session_id, context, user_row.get("text") or "")
        else:
            # Restore the scheduled run's delivery / source provenance onto the rebuilt
            # (fresh-routing) context, then run as SOURCE_SCHEDULED — not a plain user
            # turn — so suppress_delivery / the delivery target / the task attribution
            # carry through the queue (#84).
            #
            # The coalesced native-id set is split deliberately: the first id is
            # assigned to this visible harness prompt, while later ids have already
            # been preserved as hidden dedupe marker rows during the queue claim.
            if context.platform_specific is None:
                context.platform_specific = {}
            context.platform_specific.update(scheduled_prov)
            if scheduled_message_id is not None:
                # Restore the stable scheduled:/watch:/webhook: native id so the
                # flushed prompt persists + dedupes under it (Codex P2), not None.
                context.message_id = scheduled_message_id
            if pending_agent_run_ids:
                retry_agent_run_flush = False
                try:
                    with run_update_event_transaction(engine) as conn:
                        claimed_run_ids = _claim_agent_run_segment_and_retire_queue(
                            conn,
                            run_ids=pending_agent_run_ids,
                            segment=pending_scheduled_segment,
                        )
                        if set(claimed_run_ids) != set(pending_agent_run_ids):
                            queued_run_ids, stale_run_ids = inspect_queued_runs_for_workbench_in_connection(
                                conn,
                                pending_agent_run_ids,
                            )
                            stale_set = set(stale_run_ids)
                            stale_row_ids = _scheduled_segment_stale_row_ids(
                                pending_scheduled_segment,
                                set(queued_run_ids),
                            )
                            if stale_row_ids:
                                messages_service.delete_queued(conn, stale_row_ids)
                                bus.publish("queue.updated", {"session_id": session_id})
                            logger.info(
                                "queue flush: skipped coalesced agent_run segment because some runs are no longer queued: %s",
                                ",".join(sorted(set(pending_agent_run_ids) - set(claimed_run_ids))),
                            )
                            if queued_run_ids:
                                retry_agent_run_flush = True
                            else:
                                return False
                    if retry_agent_run_flush:
                        return await self.flush_queue(session_id)
                    claimed_agent_run_ids = claimed_run_ids
                    bus.publish("queue.updated", {"session_id": session_id})
                except Exception:
                    logger.warning(
                        "queue flush: failed to claim coalesced agent_run segment for session=%s",
                        session_id,
                        exc_info=True,
                    )
                    return False
            try:
                await self._run(session_id, context, scheduled_text, source=SOURCE_SCHEDULED)
            except Exception:
                if claimed_agent_run_ids:
                    try:
                        from storage.background import reset_workbench_claimed_runs_in_connection

                        with run_update_event_transaction(engine) as conn:
                            reset_workbench_claimed_runs_in_connection(conn, claimed_agent_run_ids)
                            _restore_queued_rows(conn, pending_scheduled_segment)
                    except Exception:
                        logger.exception("queue flush: failed to reset claimed agent runs for session=%s", session_id)
                logger.exception("queue flush: failed to start scheduled segment for session=%s", session_id)
                return False
            if not pending_agent_run_ids:
                if pending_scheduled_segment:
                    with engine.begin() as conn:
                        messages_service.delete_queued(conn, [r["id"] for r in pending_scheduled_segment])
                    bus.publish("queue.updated", {"session_id": session_id})
                try:
                    with engine.begin() as conn:
                        _write_coalesced_native_id_markers(conn, segment)
                except Exception:
                    logger.exception("queue flush: failed to preserve coalesced native ids for session=%s", session_id)
                    return False
        return True

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
        if session_id:
            session_ids = [session_id]
        else:
            with self._sqlite_engine().connect() as conn:
                session_ids = messages_service.list_queued_session_ids(conn)

        recovered: list[str] = []
        for queued_session_id in session_ids:
            lock = self._queue_recovery_locks.setdefault(
                queued_session_id,
                asyncio.Lock(),
            )
            async with lock:
                entry = self.in_flight.get(queued_session_id)
                if entry is not None and not entry.task.done():
                    continue
                with self._sqlite_engine().connect() as conn:
                    queue_rows = messages_service.list_queued(conn, queued_session_id)
                    if not queue_rows:
                        continue
                    head_provenance = _scheduled_provenance(queue_rows[0]) or {}
                    head_spec = head_provenance.get("platform_specific") or {}
                    if (
                        not isinstance(head_spec, dict)
                        or head_spec.get("task_trigger_kind") != "agent_run"
                    ):
                        continue
                    head_segment = _collect_scheduled_segment(queue_rows)
                    referenced_run_ids = _scheduled_segment_execution_ids(
                        head_segment
                    )
                    if not referenced_run_ids:
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

                queued_rows = [
                    row
                    for row in run_rows
                    if normalize_run_status(row["status"]) == "queued"
                ]
                live_references = {
                    str(row["id"])
                    for row in queued_rows
                    if str(row["id"]) in referenced_run_ids
                    and _run_metadata_holds_workbench_queue(row["metadata_json"])
                }
                if not live_references:
                    continue

                first_reference_at = min(
                    (
                        parsed
                        for row in queued_rows
                        if str(row["id"]) in live_references
                        and (parsed := _parse_queue_timestamp(row["created_at"]))
                        is not None
                    ),
                    default=None,
                )
                defer_to_scheduler = False
                for row in queued_rows:
                    run_id = str(row["id"])
                    if run_id in live_references:
                        continue
                    try:
                        metadata = json.loads(row["metadata_json"] or "{}")
                    except (TypeError, ValueError):
                        metadata = {}
                    if isinstance(metadata, dict) and metadata.get(
                        "workbench_queue_holds_run"
                    ):
                        continue
                    created_at = _parse_queue_timestamp(row["created_at"])
                    if (
                        first_reference_at is None
                        or created_at is None
                        or created_at <= first_reference_at
                    ):
                        defer_to_scheduler = True
                        break
                if defer_to_scheduler:
                    continue

                if await self.flush_queue(queued_session_id):
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
                pending_input_count = len(messages_service.list_queued(conn, session_id))
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

        released = 0
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
            released += 1
        if tasks_to_settle:
            await asyncio.gather(*tasks_to_settle, return_exceptions=True)
        if released:
            logger.info(
                "Released %d active Workbench turn(s) for %s runtime refresh",
                released,
                backend,
            )
        return released

    async def cancel(self, session_id: str) -> dict:
        """Stop the active turn: interrupt the agent's backend run via the SAME path
        the IM ``/stop`` command uses (Claude interrupt / Codex turn-interrupt /
        OpenCode abort) — not just the waiter — keeping the send-while-busy queue
        ("不清空"). Returns a result dict; ``code`` is ``not_in_flight`` /
        ``stop_failed`` for the HTTP adapter to map to 404 / 409, else a 200 status.
        """
        turn = self.in_flight.get(session_id)
        if turn is None:
            return {"ok": False, "code": "not_in_flight", "session_id": session_id}
        if turn.task.done():
            return {"ok": True, "session_id": session_id, "status": "already_finished"}
        # Record the no-flush intent BEFORE awaiting the interrupt: if the backend
        # stop lets the turn settle normally during the await (no CancelledError),
        # _run's finally would otherwise treat it as a natural completion and
        # flush — but a plain Stop keeps the queue (Codex P2). We pass the context the
        # turn STARTED under so the right backend is interrupted even if the Chat
        # header swapped the session's agent / model mid-turn.
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
            stale_backend = reason in {"not_active", "runtime_unavailable"}
            if stale_backend:
                # The backend no longer has an active runtime handle, but Workbench
                # still owns an in-flight waiter. Keeping it would leave Stop stuck
                # and queue future sends behind a phantom turn. Release only after
                # an explicit user Stop and keep ``stop_no_flush`` so queued
                # messages are preserved.
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
            # Stop failed/refused while the backend may still be producing output;
            # keep the Workbench turn registered so Stop remains available and
            # later natural completion can flush normally.
            turn.stop_no_flush = False
            return {"ok": False, "code": "stop_failed", "session_id": session_id, "reason": reason or None}
        backend = self._context_backend(turn.context)
        deferred = self._deferred_restart_sessions.get(backend)
        if deferred is not None:
            deferred.discard(session_id)
        turn.task.cancel()
        return {"ok": True, "session_id": session_id, "status": "cancel_requested"}

    async def send_now(self, session_id: str) -> dict:
        """Run the session's send-while-busy queue immediately ("立即发送").

        If a turn is running (and something is queued), interrupt it (the user chose
        to cut in) and opt into ``flush_on_cancel`` so the queue runs as that turn
        unwinds. If nothing is running, flush directly as a fresh turn. No-op when
        the queue is empty. Returns a result dict (``code='stop_failed'`` → 409 for
        the HTTP adapter).
        """
        turn = self.in_flight.get(session_id)
        if turn is not None and not turn.task.done():
            # Don't interrupt a live turn unless there is actually something queued to
            # cut in with — a stale queue item already flushed by another tab would
            # otherwise make send-now an unintended Stop (Codex P2).
            with self._sqlite_engine().connect() as conn:
                has_queue = bool(messages_service.list_queued(conn, session_id))
            if not has_queue:
                return {"ok": True, "session_id": session_id, "status": "empty"}
            # Record the flush intent BEFORE awaiting the interrupt (same race as
            # cancel, opposite intent: send-now WANTS the queue to run). Drop it on a
            # refused stop and leave the turn + queue untouched (Codex P2).
            turn.flush_on_cancel = True
            if turn.context.platform_specific is None:
                turn.context.platform_specific = {}
            turn.context.platform_specific["suppress_stop_no_active_notice"] = True
            stopped = False
            try:
                stopped = bool(await self.controller.command_handler.handle_stop(turn.context))
            except Exception:
                logger.exception("internal send-now: backend stop failed for session=%s", session_id)
            if not stopped:
                turn.flush_on_cancel = False
                return {"ok": False, "code": "stop_failed", "session_id": session_id}
            turn.task.cancel()
            return {"ok": True, "session_id": session_id, "status": "interrupted"}
        # No running turn — flush the queue directly as a new turn (rebuilds routing
        # from the current session row internally). ``empty`` when nothing flushed.
        flushed = await self.flush_queue(session_id)
        return {"ok": True, "session_id": session_id, "status": "flushed" if flushed else "empty"}

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

        turn_token = (getattr(context, "platform_specific", None) or {}).get("turn_token")
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
                self.pop_turn_sink(session_key, done)
                turn = self.in_flight.pop(session_id, None)
                if turn is not None:
                    bus.publish("turn.end", {"session_id": session_id})
                # Flush the send-while-busy queue on NATURAL completion (mirrors
                # ``_run``): a plain Stop keeps the queue, send_now opts back in.
                should_flush = (not cancelled and not (turn is not None and turn.stop_no_flush)) or (
                    turn is not None and turn.flush_on_cancel
                )
                if should_flush:
                    try:
                        await self.flush_queue(session_id)
                    except Exception:
                        logger.debug("agent-initiated turn: flush_queue failed", exc_info=True)

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
        run completion is still owned by the backend's terminal emit. Current
        live backends emit that terminal before ``handle_stop`` returns, and the
        bound stop context carries the original agent-run attribution so the emit
        records the run terminal. The identity guard is intentionally object-based
        so a late stop cannot complete a newer sink registered under the same
        session key.
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
        done.set()
        return True

    # --- boot / restore edge transitions -----------------------------------------

    @staticmethod
    def reset_stale() -> None:
        """Crash recovery (boot): no turn survives a restart, so any avibe session
        left ``running`` in the table is stale → reset it to ``idle`` so the sidebar
        dot doesn't show a phantom green forever. Runs in ``Controller.__init__``
        BEFORE any ``/internal/events`` subscriber exists, so it does NOT broadcast
        ``session.status`` (the bus drops events with no subscribers); the browser
        reconciles by refetching sessions when its inbox stream (re)connects."""
        try:
            from core.services import sessions as workbench_sessions_service

            engine = get_cached_sqlite_engine()
            with engine.begin() as conn:
                reset = workbench_sessions_service.reset_running_agent_status(conn)
            if reset:
                logger.info("Reset %s stale 'running' agent session(s) to idle on startup", reset)
        except Exception:
            logger.debug("agent_status startup reset failed", exc_info=True)

    def restore_running(self, session_id: Optional[str]) -> None:
        """Re-mark an avibe session ``running`` when its OpenCode poll is restored
        after a restart: the restored poll resumes the backend turn WITHOUT
        re-entering the inbound chokepoint (``handle_message``), so without this the
        dot would read idle for a still-live turn until the poll's terminal result
        settles it back. IM polls carry no workbench session id, so they pass nothing
        here and stay dot-less."""
        if session_id and self.controller is not None:
            self.controller.set_agent_status(session_id, "running")
