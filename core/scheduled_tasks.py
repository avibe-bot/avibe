"""Scheduled task persistence, parsing, and runtime orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from functools import partial, wraps
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any, Callable, Dict, Mapping, NamedTuple, Optional, Sequence, TypeVar
from uuid import uuid4
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED, JobExecutionEvent
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from config import paths
from config.atomic_io import write_atomic
from config.platform_registry import PLATFORM_REGISTRY
from config.v2_config import (
    DEFAULT_HARNESS_RUN_ORPHAN_GRACE_SECONDS,
    DEFAULT_HARNESS_RUN_QUEUED_TTL_SECONDS,
    DEFAULT_HARNESS_RUN_SWEEP_INTERVAL_SECONDS,
)
from config.v2_settings import split_thread_native_id
from core.message_context import (
    build_thread_session_anchor,
    resolve_context_platform,
    resolve_context_thread_id,
    thread_id_from_session_anchor,
)
from core.origin_links import origin_link
from core.reply_enhancer import strip_silent_blocks
from core.runtime_activation import (
    RuntimeActivationIdentity,
    RuntimeActivationResolution,
)
from core.runtime_recovery import FallbackRequestRecoveryHandler
from core.runtime_work import (
    RuntimeWorkHandler,
    RuntimeWorkItem,
    RuntimeWorkLane,
    RuntimeWorkRegistrationToken,
)
from core.run_settlement import (
    INTERRUPT_REASON_DELIVERY_TARGET_MISSING,
    RUN_INTERRUPTION_REASONS,
    SETTLEMENTS_WITHOUT_RESULT,
    SETTLED_BY_INTERRUPTED,
    SETTLED_BY_NO_TERMINAL_RESULT,
    SETTLED_BY_RESTARTED,
    SETTLED_BY_STOPPED,
    SETTLEMENT_I18N_KEYS,
    SETTLEMENT_TERMINAL_STATUS,
    SWEEP_I18N_KEYS,
)
from core.session_activities import activity_completion_output
from modules.im import MessageContext
from storage.agent_session_rows import (
    INBOX_SESSION_VISIBILITIES,
    WORKSPACE_NOTICE_SESSION_ID,
    reserve_write_lock,
    session_is_runtime_owned,
)
from storage.db import create_sqlite_engine, get_cached_sqlite_engine
from core import failure_notices
from core.backend_failure import emit_replayed_backend_failure
from core.command_runner import (
    SupervisedCommandStartupError,
    command_line_preview,
    run_supervised_command,
)
from core.delivery_evidence import (
    ACK_EVIDENCE_DELIVERY_ONLY,
    ACK_EVIDENCE_RECEIPT,
    STAGE_PERSIST,
    DeliveryEvidence,
)
from core.process_isolation import (
    PersistedProcessIdentity,
    orphaned_process_tree_presence,
    process_identity_from_payload,
    reap_orphaned_process_tree,
    serialize_process_identity,
)
from core.watch_worker import decode_watch_worker_error, localize_worker_error
from storage.background import (
    CALLBACK_TERMINAL_TURN_ID_METADATA_KEY,
    COMMAND_SNAPSHOT_METADATA_KEY,
    COMMAND_TIMED_OUT_METADATA_KEY,
    COMMAND_WORKER_METADATA_KEY,
    COMMAND_WORKER_REAP_ATTEMPTS_KEY,
    DefinitionWriteConflict,
    DefinitionWriteExpectation,
    EXECUTION_RUN_TYPES,
    NOTICE_FAILED,
    NOTICE_PENDING,
    NOTICE_SENT,
    NOTICE_SKIPPED,
    OWED_FAILURE_NOTICE_KEY,
    SKIP_REASON_SESSION_BUSY,
    SKIP_REASON_TRANSPORT_UNAVAILABLE,
    SQLiteBackgroundTaskStore,
    SWEEP_REASON_ORPHANED,
    SWEEP_REASON_QUEUE_HOLD_EXPIRED,
    SWEEP_REASON_TRANSPORT_UNAVAILABLE,
    TASK_RETIREMENT_SCHEDULE_CONSUMED,
    TASK_RETIREMENT_SCHEDULE_MISSED,
    TASK_SCHEDULE_CONSUMED_METADATA_KEY,
    TASK_LAST_RESULT_STATUS_METADATA_KEY,
    SweptRun,
    WATCH_HOOK_OUTCOME_CIRCUIT_REPAIR,
    WATCH_HOOK_OUTCOME_EVENT,
    WATCH_HOOK_OUTCOME_METADATA_KEY,
    WATCH_HOOK_OUTCOME_WAITER_FAILURE,
    compute_next_run_at,
    notice_write_expectation,
    owed_notice_eligible,
    require_task_resumable,
    resolve_run_at,
    task_schedule_generation,
)
from storage.models import agent_sessions, scope_settings, scopes
from storage.pagination import PageRequest, PageResult, page_sequence
from storage.session_reclaim import SESSION_SETTINGS_SNAPSHOT_KEY
from vibe import runtime
from vibe.i18n import t as i18n_t

logger = logging.getLogger(__name__)

_TaskStoreResult = TypeVar("_TaskStoreResult")

AGENT_RUN_DELIVERY_STEER = "steer"
AGENT_RUN_DELIVERY_QUEUE = "queue"
LEGACY_AGENT_RUN_DELIVERY_SEND_NOW = "send_now"
AGENT_RUN_DELIVERY_INTENTS = frozenset(
    {
        AGENT_RUN_DELIVERY_STEER,
        AGENT_RUN_DELIVERY_QUEUE,
    }
)
AGENT_RUN_DELIVERY_INTENT_METADATA_KEY = "delivery_intent"
AGENT_RUN_DELIVERY_OUTCOME_METADATA_KEY = "delivery_outcome"
FAILURE_CODE_SESSION_TURN_GATE_UNAVAILABLE = "session_turn_gate_unavailable"
SESSION_TURN_GATE_UNAVAILABLE_I18N_KEY = "harness.run.sessionTurnGateUnavailable"


def _publish_task_definitions_updated() -> None:
    try:
        from core.inbox_events import publish_definitions_updated

        publish_definitions_updated(definition_type="scheduled")
    except Exception:
        logger.debug("scheduled definition wake failed", exc_info=True)


class _ScopeAgentTarget(NamedTuple):
    agent_name: Optional[str]


class _ClaimedRequestBackendResolution(NamedTuple):
    authoritative: bool
    backend: str = ""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _adopt_delivery_evidence(target: DeliveryEvidence, source: DeliveryEvidence) -> None:
    """Copy one ladder rung's evidence onto the object the caller holds.

    The owed-notice drain hands ``_emit_failure_notice`` a single
    ``DeliveryEvidence`` and then reads it back, but a LADDER needs one per rung:
    ``delivered`` is a latch, so evidence shared across rungs cannot say which rung
    proved what. The walk therefore builds its own per rung and copies the decisive
    one out here, by field rather than by rebinding — the caller's reference is the
    contract.
    """

    target.delivered_id = source.delivered_id
    target.persisted_row = source.persisted_row
    target.send_returned = source.send_returned
    target.error = source.error
    target.error_stage = source.error_stage


#: How much of a failing command's stderr may ride the one-line error text. The full
#: stream is on the run row; this is only the hint the CLI list and the notice show.
_COMMAND_ERROR_DETAIL_MAX_CHARS = 200


def _last_nonempty_line(text: Optional[str]) -> str:
    """The LAST non-empty stderr line, trimmed -- the usual place the cause is.

    A failing command's stderr commonly ends with the sentence that explains the
    failure (a traceback's exception line, a shell's "command not found"), while the
    first lines are warnings and progress noise.
    """

    for line in reversed((text or "").splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped[:_COMMAND_ERROR_DETAIL_MAX_CHARS]
    return ""


def _format_seconds(seconds: float) -> str:
    """A duration as the user wrote it: ``3600``, ``0.5``, ``1.9``.

    ``--timeout`` is a float, so ``int()`` was not rounding, it was DELETING the value
    for every sub-second limit: ``--timeout 0.5`` reported "timed out after 0
    second(s)", a sentence that describes an impossible event and hides the actual
    setting the user has to change.
    """

    value = float(seconds)
    return str(int(value)) if value.is_integer() else f"{value:g}"


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def normalize_agent_run_delivery_intent(value: Any) -> str:
    """Return the durable Agent Run delivery intent or reject an unknown value."""

    from core.message_priority import normalize_delivery_intent

    normalized = normalize_delivery_intent(value or AGENT_RUN_DELIVERY_STEER)
    if normalized not in AGENT_RUN_DELIVERY_INTENTS:
        raise ValueError(f"unsupported Agent Run delivery intent: {normalized}")
    return normalized


_PathSignature = tuple[int, int, int]
_DirectoryEntrySignature = tuple[tuple[str, Optional[_PathSignature]], ...]


def _path_signature(path: Path) -> Optional[_PathSignature]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_mtime_ns, stat.st_size, stat.st_ino)


def _directory_entry_signature(path: Path) -> _DirectoryEntrySignature:
    return tuple(
        (entry.name, _path_signature(entry))
        for entry in sorted(path.glob("*.json"), key=lambda candidate: candidate.name)
    )


def _run_file_state_for_status(status: Optional[str]) -> Optional[str]:
    if status in {None, ""}:
        return None
    return {
        "queued": "pending",
        "pending": "pending",
        "running": "processing",
        "processing": "processing",
        "succeeded": "completed",
        "failed": "completed",
        "completed": "completed",
        "canceled": "completed",
        "cancelled": "completed",
    }.get(status, status)


def _normalize_requested_run_status(status: Optional[str]) -> Optional[str]:
    if status in {None, ""}:
        return None
    return {
        "pending": "queued",
        "processing": "running",
        "completed": "succeeded",
    }.get(status, status)


def _normalize_file_run_status(payload: dict[str, Any], state: str) -> str:
    raw_status = str(payload.get("status") or "").strip()
    if raw_status in {"queued", "running", "succeeded", "failed", "canceled", "cancelled"}:
        if raw_status == "cancelled":
            return "canceled"
        return raw_status
    if state == "pending":
        return "queued"
    if state == "processing":
        return "running"
    if state == "completed":
        if payload.get("ok") is False or payload.get("error"):
            return "failed"
        return "succeeded"
    return raw_status or state


TERMINAL_RUN_STATUSES = {"succeeded", "failed", "canceled"}


#: The scope types a SESSION KEY may name. A session key addresses a conversation,
#: so it is narrower than a scope id on purpose.
SESSION_KEY_SCOPE_TYPES = frozenset({"channel", "user"})
#: The scope types a SCOPE ID may name — the same two plus the workbench's
#: ``project``, which is not a conversation and cannot carry a thread.
SCOPE_ID_SCOPE_TYPES = frozenset({"channel", "user", "project"})
#: Every scope type a failure-notice delivery target can carry, because ``_add``
#: builds every rung through exactly those two parsers and neither admits anything
#: else. One of the two axes of ``LADDER_ACK_SOURCES`` below, named here rather
#: than spelled inline in the parsers so the acknowledgement policy can be checked
#: against the real vocabulary instead of a hand-copied echo of it.
LADDER_SCOPE_TYPES = SESSION_KEY_SCOPE_TYPES | SCOPE_ID_SCOPE_TYPES


@dataclass(frozen=True)
class ParsedSessionKey:
    platform: str
    scope_type: str
    scope_id: str
    thread_id: Optional[str] = None

    @property
    def session_scope(self) -> str:
        return f"{self.platform}::{self.scope_type}::{self.scope_id}"

    @property
    def is_dm(self) -> bool:
        return self.scope_type == "user"

    def to_key(self, *, include_thread: bool = True) -> str:
        base = f"{self.platform}::{self.scope_type}::{self.scope_id}"
        if include_thread and self.thread_id:
            return f"{base}::thread::{self.thread_id}"
        return base


def parse_session_key(value: str) -> ParsedSessionKey:
    raw = (value or "").strip()
    parts = raw.split("::") if raw else []
    if len(parts) not in {3, 5}:
        raise ValueError("session key must be '<platform>::<channel|user>::<id>[::thread::<thread_id>]'")

    platform, scope_type, scope_id = parts[:3]
    if not platform or not scope_id:
        raise ValueError("session key platform and scope id are required")
    if scope_type not in SESSION_KEY_SCOPE_TYPES:
        raise ValueError("session key scope type must be 'channel' or 'user'")

    thread_id: Optional[str] = None
    if len(parts) == 5:
        if parts[3] != "thread" or not parts[4]:
            raise ValueError("session key thread segment must be '::thread::<thread_id>'")
        thread_id = parts[4]

    return ParsedSessionKey(
        platform=platform,
        scope_type=scope_type,
        scope_id=scope_id,
        thread_id=thread_id,
    )


def parse_scope_id(value: str) -> ParsedSessionKey:
    raw = (value or "").strip()
    parts = raw.split("::") if raw else []
    if len(parts) != 3:
        raise ValueError("scope id must be '<platform>::<scope_type>::<native_id>'")

    platform, scope_type, native_id = parts
    if not platform or not scope_type or not native_id:
        raise ValueError("scope id platform, scope type, and native id are required")
    if scope_type not in SCOPE_ID_SCOPE_TYPES:
        raise ValueError("scope id scope type must be 'channel', 'user', or 'project'")

    return ParsedSessionKey(
        platform=platform,
        scope_type=scope_type,
        scope_id=native_id,
        thread_id=None,
    )


# --- the failure-notice ladder's target x acknowledgement policy -------------
#
# WHICH evidence is allowed to acknowledge an owed failure notice depends on WHAT
# was addressed, and getting that wrong in the permissive direction is the worst
# outcome the drain has: a notice marked ``sent`` with nothing durable behind it is
# lost forever, which is strictly worse than the visible dead letter it replaces.
#
# It was three separate review findings before it was a table — an ``avibe``
# special case bolted onto a boolean, which answered for the target classes anyone
# had thought about and fell through to the permissive branch for the rest. So the
# policy is ENUMERATED over the two axes a target actually has, the lookup is
# TOTAL, and the answer for a class nobody declared is the strict one.

#: The transport returned an id that the PLATFORM minted, so the id itself is proof
#: a person was told; re-sending because the bookkeeping write failed afterwards
#: would spam a notice that already arrived. A persisted receipt is admitted too —
#: it is the same claim, only stronger.
ACK_SOURCE_NATIVE_DELIVERY_ID = "native_delivery_id"
#: Only a durable ``messages`` row acknowledges. For the workbench that is not
#: strictness for its own sake, it is what delivery MEANS: the inbox reads rows, an
#: SSE fan-out with no browser attached reaches nobody, and
#: ``AvibeBot.send_message`` mints and returns a synthetic ``msg_<hex>`` id
#: unconditionally — no subscriber required and nothing persisted. Its id therefore
#: proves nothing at all. Includes the dedup receipt: the duplicate short-circuit
#: reports the row it FOUND (``persist_agent_message`` already committed it before
#: the crash), which is the strongest receipt there is, so a crash-then-retry on a
#: workbench rung acknowledges instead of re-sending forever.
ACK_SOURCE_PERSISTED_RECEIPT = "persisted_receipt"

#: Which ``DeliveryEvidence.ack_evidence`` values each source admits. Spelled as
#: sets rather than as a comparison so a third evidence strength cannot be added
#: without deciding, per source, whether it acknowledges.
ACK_EVIDENCE_BY_ACK_SOURCE: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        ACK_SOURCE_NATIVE_DELIVERY_ID: frozenset(
            {ACK_EVIDENCE_RECEIPT, ACK_EVIDENCE_DELIVERY_ONLY}
        ),
        ACK_SOURCE_PERSISTED_RECEIPT: frozenset({ACK_EVIDENCE_RECEIPT}),
    }
)

#: The platform-kind of a target naming a platform the registry does not know. A
#: deliver key is a free string and ``parse_session_key`` does not check it against
#: the registry, so this class is reachable and needs a name rather than an
#: accident.
LADDER_PLATFORM_KIND_UNREGISTERED = "unregistered"

#: THE policy: one row per (platform kind, scope type) a ladder target can be.
#: ``PLATFORM_REGISTRY``'s ``kind`` is the axis, not the platform id, so a new IM
#: transport inherits the IM answer instead of needing a row of its own — and a new
#: KIND (a transport that is neither, say a future cloud relay) does need one, and
#: ``test_every_ladder_target_class_declares_its_acknowledgement_source`` fails
#: until it gets one.
#:
#: THE TRUST BOUNDARY THAT MAKES THAT SAFE, stated because it is a premise this table
#: cannot check for itself: ``kind == "im"`` means the id a send RETURNS was minted by
#: a platform that reached a person. That is what entitles the two ``im`` conversation
#: rows below to acknowledge on a delivery id alone. A transport that mints its own id
#: locally does not qualify, however IM-shaped it looks — the workbench is exactly that
#: case (``AvibeBot.send_message`` returns a synthetic ``msg_<hex>`` unconditionally),
#: which is why it has a kind of its own rather than a platform-id exception here.
#: ``PlatformDescriptor.kind`` DEFAULTS to ``"im"``, so a transport added to the
#: registry without stating its kind would silently take the permissive rows and no
#: structural test here would notice: the kind axis would be unchanged, the table would
#: still be total. ``test_every_registry_platform_declares_its_kind_explicitly``
#: guards that specific accident at the registry, where the claim is made.
LADDER_ACK_SOURCES: Mapping[tuple[str, str], str] = MappingProxyType(
    {
        # A real IM conversation: the send id came from Slack/Discord/Telegram/
        # Lark/WeChat, so it is evidence the user was told.
        ("im", "channel"): ACK_SOURCE_NATIVE_DELIVERY_ID,
        ("im", "user"): ACK_SOURCE_NATIVE_DELIVERY_ID,
        # An IM platform with a ``project`` scope is not a conversation — the id is
        # an internal scope row, not a native channel — so a returned id does not
        # locate a person. Reachable through a hand-written ``deliver_key`` or
        # creator scope, and receipt-gated rather than trusted.
        ("im", "project"): ACK_SOURCE_PERSISTED_RECEIPT,
        # The workbench, uniformly, for every scope type: the reason is the
        # TRANSPORT (a synthetic id minted whether or not anything landed), not the
        # scope shape, so ``avibe::user::…`` from a workbench creator's provenance
        # is exactly as unproven as ``avibe::project::…``.
        ("workbench", "channel"): ACK_SOURCE_PERSISTED_RECEIPT,
        ("workbench", "user"): ACK_SOURCE_PERSISTED_RECEIPT,
        ("workbench", "project"): ACK_SOURCE_PERSISTED_RECEIPT,
        # A platform the registry has never heard of: nothing is known about what
        # its send id means, so nothing is assumed.
        (LADDER_PLATFORM_KIND_UNREGISTERED, "channel"): ACK_SOURCE_PERSISTED_RECEIPT,
        (LADDER_PLATFORM_KIND_UNREGISTERED, "user"): ACK_SOURCE_PERSISTED_RECEIPT,
        (LADDER_PLATFORM_KIND_UNREGISTERED, "project"): ACK_SOURCE_PERSISTED_RECEIPT,
    }
)

#: The answer for a target class the table does not name. Deliberately the STRICT
#: source: an undeclared class that acked on a send id would lose the notice
#: permanently, while one that demands a receipt it cannot produce retries and then
#: dead-letters — visibly, with ``last_error``, the health badge and
#: ``vibe task show`` all still reporting the failure.
UNDECLARED_LADDER_ACK_SOURCE = ACK_SOURCE_PERSISTED_RECEIPT


def failure_notice_target_class(target: ParsedSessionKey) -> tuple[str, str]:
    """Which enumerated class this ladder target belongs to.

    Total by construction: an unregistered platform gets a named kind, and
    ``scope_type`` comes from a parser whose vocabulary is ``LADDER_SCOPE_TYPES``.
    """

    descriptor = PLATFORM_REGISTRY.get(target.platform)
    kind = descriptor.kind if descriptor is not None else LADDER_PLATFORM_KIND_UNREGISTERED
    return (kind, target.scope_type)


def failure_notice_ack_source(target: ParsedSessionKey) -> str:
    """The one evidence source that may acknowledge a notice sent to *target*."""

    return LADDER_ACK_SOURCES.get(
        failure_notice_target_class(target),
        UNDECLARED_LADDER_ACK_SOURCE,
    )


def session_anchor_for_target(target: ParsedSessionKey) -> str:
    if target.thread_id:
        return build_thread_session_anchor(target.platform, target.scope_id, target.thread_id)
    return f"{target.platform}_{target.scope_id}"


@dataclass(frozen=True)
class ResolvedSessionIdTarget:
    session_id: str
    session_key: ParsedSessionKey
    agent_backend: str
    agent_variant: str
    native_session_id: str
    scope_id: Optional[str] = None
    visibility: str = "foreground"
    agent_id: Optional[str] = None
    agent_name: Optional[str] = None
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    workdir: Optional[str] = None
    session_anchor: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    suppress_delivery: bool = False


@dataclass(frozen=True)
class TaskExecutionResult:
    error: Optional[str]
    session_key: str
    session_id: Optional[str]
    failure_code: Optional[str] = None
    complete_on_return: bool = True
    reconcile_delivery_on_return: bool = False
    #: Command-task outcome facts, all ``None`` for a message task. They ride the
    #: result rather than being written by the executor so the run row's terminal
    #: write stays a single guarded statement in ``complete()``.
    exit_code: Optional[int] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    timed_out: Optional[bool] = None
    #: The Agent turn a failed ``--on-failure agent`` command fire queued, already
    #: durable when this is set. It rides the run row's metadata so the settle that
    #: transitions the run also records that its failure has a REPORT, which is what
    #: suppresses the owed failure notice for the same failure.
    escalation_run_id: Optional[str] = None


@dataclass(frozen=True)
class TaskDispatchResult:
    """Admission result for any Task/Watch/Hook input submission."""

    error: Optional[str]
    complete_on_return: bool = True


@dataclass(frozen=True)
class AgentRunExecutionResult:
    error: Optional[str]
    complete_on_return: bool
    failure_code: Optional[str] = None
    requeue_on_return: bool = False
    recover_queue_on_return: bool = False
    # The run's terminal row was already written by the executor itself (through a
    # guarded writer), so the caller must skip ``complete()`` but still run the
    # post-completion side effects: callback delivery and session-queue recovery.
    settled_out_of_band: bool = False
    delivery_outcome: Optional[dict[str, Any]] = None


#: Durable definition-metadata key recording the last binding recovery, so a
#: definition that keeps hitting the same dead session is reported once and not
#: once per cron minute.
BINDING_RECOVERY_METADATA_KEY = "binding_recovery"

#: Stable terminal code written only after the target resolver has proved that
#: a pinned Session cannot accept a turn. Human-readable error text is allowed
#: to evolve; the three-failure auto-pause policy must never parse it.
FAILURE_CODE_UNRESOLVABLE_TARGET = "unresolvable_target"
UNRESOLVABLE_TARGET_AUTO_PAUSE_FAILURES = 3

#: Durable definition-metadata flag: this definition deliberately pins NO Agent
#: of its own and follows whatever Agent its bound Session carries.
#:
#: A reset rebind clears ``agent_name`` because the Agent the definition pinned
#: is the one that was just found unusable. An absent ``agent_name`` alone cannot
#: say that: it is also what "the user never pinned one" looks like, and
#: ``vibe task update`` re-resolves an omitted Agent for every non-``existing``
#: policy and writes the result back -- so an unrelated ``--name`` edit silently
#: re-pinned an Agent the recovery had deliberately dropped, and the definition
#: went back to dispatching under it. The flag makes "follow the Session" an
#: explicit, durable state that ordinary edits preserve; an explicit ``--agent``
#: clears it (the user is pinning again).
BINDING_FOLLOWS_SESSION_METADATA_KEY = "binding_follows_session"

#: Durable definition-metadata list of reserved sessions this definition's own
#: recovery path handed out and then could NOT give back (HFR-276).
#:
#: ``_release_reserved_session`` is best-effort by design -- it runs on a path that
#: is already reporting a failure and must not raise a second one -- so a locked
#: database or an I/O fault leaves it returning ``False`` with the reservation still
#: live. HFR-270's promise (a refused rebind leaves nothing behind) then depends on
#: a cleanup that may not have happened, and NOTHING named the row: its id is random,
#: it is never written to the definition, and the next fire that loses the same race
#: reserves and leaks another one.
#:
#: Recording the id here converts an untracked leak into a tracked, retryable one.
#: The definition that reserved it is the only thing that ever knew the id, the row
#: is where the binding-recovery record already lives, and it survives a restart --
#: so the next fire of the same definition can retry the release
#: (``_retry_orphaned_reservations``) and drop the entries it resolves.
#: ``ScheduledTaskStore.list_orphaned_reservations`` is the read side.
#:
#: Each entry: ``{"session_id": str, "reason": str, "at": iso8601}``.
ORPHANED_RESERVATIONS_METADATA_KEY = "orphaned_reservations"

#: Wall-clock ceiling applied to a command task that stored no ``timeout_seconds``.
#: Six hours: long enough that an ordinary batch job is never cut short, short
#: enough that a wedged child cannot hold a run row ``running`` forever. A stored
#: ``0`` is NOT this default -- it flows through the runner as "no timeout".
COMMAND_TASK_DEFAULT_TIMEOUT_SECONDS = 21600.0

#: Retained bytes per output stream for one command run. The runner keeps draining
#: the child past the cap, so a chatty command is truncated in the run row rather
#: than deadlocked on a full pipe.
COMMAND_TASK_OUTPUT_CAP_BYTES = 64 * 1024

def command_snapshot(task: Any) -> Optional[dict[str, Any]]:
    """The ``{"shell", "argv"}`` record of what a command definition would run.

    A run row carried an exit code and output but never the command, so every reader
    -- the failure notice, ``vibe runs show``, the Workbench run detail -- had to go
    back to the definition for the copy. The definition is mutable and deletable, and
    the notice drain is asynchronous, so by the time a reader asked, the answer could
    be a command that never ran (the user rewrote it after the fire) or no answer at
    all (the user deleted the task). A run is an immutable record of one execution;
    what it executed belongs ON it.

    The SQLite backend stamps the same key from the authoritative definition row
    inside its enqueue transaction; this is the file backend's copy of that write.
    ``None`` for a definition that has no command, so a caller can merge the result
    unconditionally without stamping an empty key onto every Agent run's metadata.
    """

    if not getattr(task, "has_command", False):
        return None
    shell_command = getattr(task, "shell_command", None)
    argv = getattr(task, "command", None)
    return {
        "shell": shell_command or None,
        "argv": [str(part) for part in (argv or [])],
    }


def command_snapshot_of_run(run: Any) -> Optional[SimpleNamespace]:
    """The snapshot a run carries, shaped like the definition readers already accept.

    Returns an object exposing ``has_command`` / ``shell_command`` / ``command`` so
    the preview helpers take it and a ``ScheduledTask`` through the same code path.
    ``None`` when the run predates the snapshot or is not a command run at all -- the
    callers fall back to the live definition there, which is the best (and, for those
    rows, the only) answer available.
    """

    if not isinstance(run, dict):
        return None
    metadata = run.get("metadata")
    if not isinstance(metadata, dict):
        return None
    snapshot = metadata.get(COMMAND_SNAPSHOT_METADATA_KEY)
    if not isinstance(snapshot, dict):
        return None
    shell_command = snapshot.get("shell")
    raw_argv = snapshot.get("argv")
    argv = [str(part) for part in raw_argv] if isinstance(raw_argv, (list, tuple)) else []
    shell_command = str(shell_command) if isinstance(shell_command, str) else None
    if not shell_command and not argv:
        return None
    return SimpleNamespace(
        has_command=True,
        shell_command=shell_command,
        command=argv,
    )

#: What a fire reports when its terminal stamp was REFUSED by the guarded
#: full-row write (HFR-261/HFR-264). Translated, not plain text: this is a sentence
#: THIS MODULE composes for a user, and it lands in the run ledger's ``error`` --
#: the same column ``harness.command.timedOut`` / ``exited`` and the settlement
#: reasons in ``core/run_settlement.py`` already fill through ``vibe/i18n``, rendered
#: verbatim in the Harness detail pane, the CLI and the failure notice. The values
#: that stay untranslated on this channel are the ones nobody here wrote -- ``str(exc)``
#: from an arbitrary exception, a worker's own stderr -- so "one convention per
#: channel" is the wrong reading: the line is between OUR copy and QUOTED text.
_TASK_RESULT_NOT_RECORDED_I18N_KEY = "harness.task.resultNotRecorded"

#: "No value was supplied", as distinct from "the supplied value is ``None``".
#: A reclaim snapshot records a session's ``model`` / ``reasoning_effort`` as
#: NULL when the session pinned neither, and D3 requires the rebind to write
#: that NULL through unchanged. Collapsing both meanings into ``None`` makes a
#: session silently acquire whatever model its Agent happens to carry at rebind
#: time, while the recovery is still recorded as settings-preserving. Mirrors
#: the ``_UNSET`` sentinel in ``core/vibe_agents.py``.
_UNSET: Any = object()


class UnresolvableSessionTarget(ValueError):
    """A pinned ``session_id`` that can never resolve until something rebinds it.

    A distinct type rather than a message match, because it selects the one error
    class a definition may be auto-paused or rebound for. Transient faults (a DB
    error, a refused turn) must NOT land here: pausing a user's task because
    SQLite was momentarily unavailable is a worse bug than the one this fixes.

    Subclasses ``ValueError`` so every existing ``except ValueError`` caller —
    the CLI, the API, watches — keeps its current behaviour.
    """

    def __init__(self, message: str, *, session_id: str, reason: str) -> None:
        super().__init__(message)
        self.session_id = session_id
        self.reason = reason


@dataclass
class SessionBindingChange:
    """What the scheduler did about a pinned session that no longer exists."""

    # "rebound"   -- a replacement session was reserved AND stored
    # "failing"   -- a user-pinned binding failed below the auto-pause threshold
    # "paused"    -- the definition was user-pinned, so it was disabled instead
    # "reclaimed" -- a replacement was reserved but the guarded write refused it, so
    #                nothing was stored and the fire did not run (HFR-268), AND the
    #                replacement was given back (HFR-270)
    # "orphaned"  -- the same refusal, but giving the replacement back FAILED, so the
    #                session is still live and is recorded for a later attempt
    #                (HFR-276). Distinct from "reclaimed" because the cleanup is the
    #                only difference between them and the user is told which happened.
    action: str
    task_id: str
    reason: str
    previous_session_id: Optional[str]
    detail: str
    new_session_id: Optional[str] = None
    settings_preserved: bool = False
    #: The reserved session that could not be released, on the "orphaned" action.
    orphaned_session_id: Optional[str] = None
    #: Whether that id was durably recorded on the definition for a later attempt.
    orphan_tracked: bool = False

    @property
    def signature(self) -> str:
        """One broken binding, one notification — not one per fire."""

        return f"{self.action}:{self.reason}:{self.previous_session_id or ''}"


def resolve_session_id_target(session_id: str, *, db_path: Optional[Path] = None) -> ResolvedSessionIdTarget:
    raw = (session_id or "").strip()
    if not raw:
        raise ValueError("session id is required")

    engine = create_sqlite_engine(db_path or paths.get_sqlite_state_path())
    try:
        with engine.connect() as conn:
            row = conn.execute(
                select(
                    agent_sessions.c.id,
                    agent_sessions.c.scope_id,
                    agent_sessions.c.status,
                    agent_sessions.c.visibility,
                    agent_sessions.c.agent_id,
                    agent_sessions.c.agent_name,
                    agent_sessions.c.agent_backend,
                    agent_sessions.c.agent_variant,
                    agent_sessions.c.model,
                    agent_sessions.c.reasoning_effort,
                    agent_sessions.c.session_anchor,
                    agent_sessions.c.workdir,
                    agent_sessions.c.native_session_id,
                    scopes.c.platform,
                    scopes.c.scope_type,
                    scopes.c.native_id,
                    agent_sessions.c.metadata_json.label("session_metadata_json"),
                )
                .join(scopes, scopes.c.id == agent_sessions.c.scope_id, isouter=True)
                .where(agent_sessions.c.id == raw)
                .limit(1)
            ).mappings().first()
    except SQLAlchemyError as exc:
        raise ValueError(f"agent session id not found: {raw}") from exc
    finally:
        engine.dispose()

    if row is None:
        raise UnresolvableSessionTarget(
            f"agent session id not found: {raw}", session_id=raw, reason="missing"
        )
    # A RUNTIME-OWNED session accepts no turn from anybody, so it is not a target.
    #
    # THE ROUTE-LOCAL GUARD WAS NOT ENOUGH (review thread 3678900318). Refusing the
    # send in ``POST /api/sessions/<id>/messages`` closed the composer, which is the
    # door a human finds; it left every OTHER turn entry point open, because they all
    # come through HERE instead. ``vibe agent run --session-id ses-workspace-notices``
    # (``cmd_agent_run`` resolves the pin at ``vibe/cli.py``'s ``session_policy in
    # {"existing", "fork"}`` branch), a task or watch pinned with ``--session-id``, and
    # ``enqueue_session_callback`` all resolve first and enqueue a real turn second. So
    # the ownership check belongs in the SHARED resolver: one line, and every present
    # and future backend entry point inherits the no-turn contract instead of having to
    # remember it. Same reasoning as ``archive_session`` / ``update_session`` owning the
    # write refusals rather than each caller.
    #
    # BEFORE the archived check, deliberately, even though the two barely overlap. The
    # reserved row may not be archived at all (``archive_session`` refuses its id) and
    # ``resolve_workspace_notice_session`` heals a corrupted ``archived`` status on the
    # next notice, so the ordering only decides which refusal a CORRUPTED row reports
    # in that window — and "reserved for the runtime" is both the more specific fact
    # and the one that stays true after the heal. Existence still wins over both: a
    # missing row is ``missing``, whatever its id claimed to be.
    #
    # ``reason="reserved"`` IS A NEW VALUE, not a reuse of the two above, and it is
    # deliberately left OUT of the ``delivery_target_missing`` classification in
    # ``_execute_claimed_request`` (which keys on ``missing`` alone). This row exists
    # and is healthy; nothing about the DESTINATION is dead. Pointing a definition at
    # it is a CONFIGURATION error, and labelling it as a vanished delivery target would
    # send the reader looking for a session that is sitting right there in their inbox.
    # The run still settles ``failed`` naming the reserved session, which is the
    # visible outcome that matters.
    if session_is_runtime_owned(session_id=raw, visibility=row["visibility"]):
        raise UnresolvableSessionTarget(
            f"agent session is reserved for the runtime and accepts no turn: {raw}",
            session_id=raw,
            reason="reserved",
        )
    # Archived sessions are terminal + inert. A task/watch/run that still targets
    # one by id must NOT fire into it — treat it as an unresolvable target so the
    # run is skipped (archive also reclaims bound definitions, so this is defense
    # in depth for manual ``--session-id`` runs and any stragglers).
    if str(row["status"] or "") == "archived":
        raise UnresolvableSessionTarget(
            f"agent session is archived: {raw}", session_id=raw, reason="archived"
        )
    persisted_scope_id = str(row["scope_id"] or "").strip() or None
    platform = str(row["platform"] or "")
    scope_type = str(row["scope_type"] or "")
    native_scope_id = str(row["native_id"] or "")
    # ``project`` is the avibe workbench's scope type (sessions live under
    # ``avibe::project::proj_<hex>``). A session-id target carries the concrete
    # ``session_id`` (the row PK) regardless of scope type, and the dispatch binds
    # the reply to that reserved session via ``agent_session_target`` — so a
    # project-scoped row IS a valid task target. (``--session-key`` targeting stays
    # channel/user-only: a bare project key wouldn't identify a single session.)
    scoped_thread_id: Optional[str] = None
    if persisted_scope_id is None:
        platform = "avibe"
        scope_type = "project"
        native_scope_id = raw
    else:
        if not platform or not native_scope_id:
            raise UnresolvableSessionTarget(
                f"agent session id cannot be used as a task target: {raw}",
                session_id=raw,
                reason="unusable",
            )
        if scope_type == "thread":
            try:
                native_scope_id, scoped_thread_id = split_thread_native_id(native_scope_id)
            except ValueError as exc:
                raise UnresolvableSessionTarget(
                    f"agent session id cannot be used as a task target: {raw}",
                    session_id=raw,
                    reason="unusable",
                ) from exc
            scope_type = "channel"
        elif scope_type not in {"channel", "user", "project"}:
            raise UnresolvableSessionTarget(
                f"agent session id cannot be used as a task target: {raw}",
                session_id=raw,
                reason="unusable",
            )

    anchor = str(row["session_anchor"] or "")
    thread_id = scoped_thread_id
    if thread_id is None and persisted_scope_id is not None and anchor != raw:
        thread_id = _thread_id_from_session_anchor(anchor, platform=platform, scope_id=native_scope_id)
    session_metadata = _json_loads(row["session_metadata_json"], {})
    visibility = str(row["visibility"] or "foreground")
    return ResolvedSessionIdTarget(
        session_id=raw,
        scope_id=persisted_scope_id,
        visibility=visibility,
        session_key=ParsedSessionKey(
            platform=platform,
            scope_type=scope_type,
            scope_id=native_scope_id,
            thread_id=thread_id,
        ),
        agent_backend=str(row["agent_backend"] or ""),
        agent_variant=str(row["agent_variant"] or ""),
        agent_id=row["agent_id"],
        agent_name=row["agent_name"],
        model=row["model"],
        reasoning_effort=row["reasoning_effort"],
        native_session_id=str(row["native_session_id"] or ""),
        workdir=row["workdir"],
        session_anchor=str(row["session_anchor"] or ""),
        metadata=session_metadata if isinstance(session_metadata, dict) else {},
        suppress_delivery=visibility == "background",
    )


def enqueue_session_callback(
    request_store: "TaskExecutionStore",
    *,
    session_id: str,
    message: str,
    source_actor: str,
    source_session_id: Optional[str] = None,
    parent_run_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> Optional["TaskExecutionRequest"]:
    """Enqueue a callback turn into an existing agent session — the shared entry used by Agent
    Run / watch / scheduled-task callbacks and vault-request auto-resume. Resolves the session's
    agent/backend/model target and enqueues an ``agent_run`` with ``source_kind="callback"`` so
    the running scheduler dispatches it. Returns ``None`` when there is nothing to send;
    ``resolve_session_id_target`` raises for an unresolvable/archived session (caller handles).
    """
    session_id = (session_id or "").strip()
    if not session_id or not (message or "").strip():
        return None
    target = resolve_session_id_target(session_id)
    from core.message_priority import delivery_intent_for_trigger

    callback_metadata = dict(metadata or {})
    if source_session_id:
        callback_metadata["source_session_id"] = str(source_session_id)

    return request_store.enqueue_agent_run(
        session_id=session_id,
        session_key=target.session_key.to_key(),
        message=message,
        agent_name=target.agent_name,
        agent_id=target.agent_id,
        agent_backend=target.agent_backend,
        model=target.model,
        reasoning_effort=target.reasoning_effort,
        session_policy="existing",
        source_kind="callback",
        source_actor=source_actor,
        parent_run_id=parent_run_id,
        delivery_intent=delivery_intent_for_trigger("callback"),
        metadata={
            **callback_metadata,
            **({"callback_parent_run_id": parent_run_id} if parent_run_id else {}),
        },
        callback_parent_to_arm=parent_run_id,
    )


def _thread_id_from_session_anchor(anchor: str, *, platform: str, scope_id: str) -> Optional[str]:
    return thread_id_from_session_anchor(anchor, platform=platform, channel_id=scope_id)


def build_session_key_for_context(
    context: MessageContext,
    *,
    include_thread: bool = False,
    fallback_platform: Optional[str] = None,
) -> ParsedSessionKey:
    payload = context.platform_specific or {}
    platform = resolve_context_platform(context, fallback_platform=fallback_platform)
    is_dm = bool(payload.get("is_dm", False))
    scope_type = "user" if is_dm else "channel"
    scope_id = context.user_id if is_dm else context.channel_id
    return ParsedSessionKey(
        platform=platform,
        scope_type=scope_type,
        scope_id=scope_id,
        thread_id=(resolve_context_thread_id(context) or context.thread_id) if include_thread else None,
    )


def _created_by_caller(task: Any, run_metadata: Any) -> Optional[dict[str, Any]]:
    """The ``created_by.caller`` provenance a definition was created with, or ``None``.

    Read from the DEFINITION's metadata when the definition row still exists and from
    the run's own copy otherwise, so a deleted definition keeps whatever the run
    recorded. Shared by the failure ladder (rungs 3 and 4, which ADDRESS the origin) and
    the notice body (which NAMES it): the two must never disagree about who created a
    definition, and before this they read the same nested shape from two places.
    """

    source = (getattr(task, "metadata", None) if task is not None else run_metadata) or {}
    created_by = source.get("created_by") if isinstance(source, dict) else None
    caller = created_by.get("caller") if isinstance(created_by, dict) else None
    return caller if isinstance(caller, dict) else None


def _coerce_command_argv(value: Any) -> Optional[list[str]]:
    """An argv list of strings, or ``None`` for absent/empty/malformed input.

    ``[]`` collapses to ``None`` on purpose: an empty argv is not a command, and the
    storage layer must keep the column NULL so ``has_command`` stays False.
    """

    if not isinstance(value, list) or not value:
        return None
    if not all(isinstance(item, str) for item in value):
        return None
    return [str(item) for item in value]


def _coerce_optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class ScheduledTask:
    id: str
    name: Optional[str]
    session_key: str
    prompt: str
    schedule_type: str
    agent_name: Optional[str] = None
    session_policy: Optional[str] = None
    session_id: Optional[str] = None
    post_to: Optional[str] = None
    deliver_key: Optional[str] = None
    cwd: Optional[str] = None
    cron: Optional[str] = None
    run_at: Optional[str] = None
    timezone: str = "UTC"
    enabled: bool = True
    created_at: str = field(default_factory=_utc_now_iso)
    updated_at: str = field(default_factory=_utc_now_iso)
    last_run_at: Optional[str] = None
    last_run_id: Optional[str] = None
    retired_at: Optional[str] = None
    retirement_reason: Optional[str] = None
    last_error: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Command tasks run a subprocess instead of messaging an Agent. All four stay
    # ``None`` for a message task, which is what keeps its stored columns NULL.
    shell_command: Optional[str] = None
    command: Optional[list[str]] = None
    timeout_seconds: Optional[float] = None
    last_exit_code: Optional[int] = None

    @property
    def has_command(self) -> bool:
        """Whether this definition runs a command rather than prompting an Agent."""

        return bool(self.shell_command or self.command)

    @property
    def on_failure(self) -> str:
        """What a failed command run should do; ``"none"`` when unset.

        Kept in ``metadata`` rather than a column: it is a command-task-only policy,
        and the CLI owns validating which values are accepted.
        """

        value = str((self.metadata or {}).get("on_failure") or "none").strip().lower()
        return value or "none"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ScheduledTask":
        return cls(
            id=str(payload.get("id") or uuid4().hex[:12]),
            name=(str(payload["name"]).strip() if payload.get("name") is not None else None) or None,
            session_key=str(payload.get("session_key") or ""),
            prompt=str(payload.get("prompt") or ""),
            schedule_type=str(payload.get("schedule_type") or ""),
            agent_name=(str(payload["agent_name"]).strip() if payload.get("agent_name") else None),
            session_policy=(str(payload["session_policy"]).strip() if payload.get("session_policy") else None),
            session_id=(str(payload["session_id"]).strip() if payload.get("session_id") else None),
            post_to=payload.get("post_to"),
            deliver_key=payload.get("deliver_key"),
            cwd=(str(payload["cwd"]).strip() if payload.get("cwd") else None) or None,
            cron=payload.get("cron"),
            run_at=payload.get("run_at"),
            timezone=str(payload.get("timezone") or "UTC"),
            enabled=bool(payload.get("enabled", True)),
            created_at=str(payload.get("created_at") or _utc_now_iso()),
            updated_at=str(payload.get("updated_at") or _utc_now_iso()),
            last_run_at=payload.get("last_run_at"),
            last_run_id=payload.get("last_run_id"),
            retired_at=payload.get("retired_at"),
            retirement_reason=payload.get("retirement_reason"),
            last_error=payload.get("last_error"),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
            shell_command=(str(payload["shell_command"]) if payload.get("shell_command") else None),
            command=_coerce_command_argv(payload.get("command")),
            timeout_seconds=_coerce_optional_float(payload.get("timeout_seconds")),
            last_exit_code=_coerce_optional_int(payload.get("last_exit_code")),
        )


@dataclass
class TaskExecutionRequest:
    id: str
    request_type: str
    created_at: str = field(default_factory=_utc_now_iso)
    task_id: Optional[str] = None
    session_key: Optional[str] = None
    session_id: Optional[str] = None
    post_to: Optional[str] = None
    deliver_key: Optional[str] = None
    prompt: Optional[str] = None
    message: Optional[str] = None
    message_payload: Any = None
    source_kind: Optional[str] = None
    source_actor: Optional[str] = None
    parent_run_id: Optional[str] = None
    agent_name: Optional[str] = None
    agent_id: Optional[str] = None
    agent_backend: Optional[str] = None
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    session_policy: Optional[str] = None
    callback_session_id: Optional[str] = None
    callback_status: Optional[str] = None
    delivery_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    observed_activation_identity: RuntimeActivationIdentity | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(replace(self, observed_activation_identity=None))
        payload.pop("observed_activation_identity", None)
        return payload

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "TaskExecutionRequest":
        return cls(
            id=str(payload.get("id") or uuid4().hex[:12]),
            request_type=str(payload.get("request_type") or ""),
            created_at=str(payload.get("created_at") or _utc_now_iso()),
            task_id=payload.get("task_id"),
            session_key=payload.get("session_key"),
            session_id=payload.get("session_id"),
            post_to=payload.get("post_to"),
            deliver_key=payload.get("deliver_key"),
            prompt=payload.get("prompt"),
            message=payload.get("message") or payload.get("prompt"),
            message_payload=payload.get("message_payload"),
            source_kind=payload.get("source_kind"),
            source_actor=payload.get("source_actor"),
            parent_run_id=payload.get("parent_run_id"),
            agent_name=payload.get("agent_name"),
            agent_id=payload.get("agent_id"),
            agent_backend=payload.get("agent_backend"),
            model=payload.get("model"),
            reasoning_effort=payload.get("reasoning_effort"),
            session_policy=payload.get("session_policy"),
            callback_session_id=payload.get("callback_session_id"),
            callback_status=payload.get("callback_status"),
            delivery_id=payload.get("delivery_id"),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        )


def _agent_run_message_for_request(request: TaskExecutionRequest) -> str:
    return str(request.message or "")


def _retire_stale_agent_run_queue_rows(
    *,
    session_id: Optional[str],
    execution_ids: list[str],
    resubmit_execution_id: Optional[str] = None,
) -> int:
    """Retire old queued Workbench rows for recovered direct Agent Runs.

    A crash can happen after the run rows are claimed but before flush_queue
    deletes the queued harness rows. On restart the primary run is recovered and
    submitted here; leaving the old queued rows in place makes their native ids
    look like delivered duplicates even though they are only stale queue state.
    Child rows still need their native ids preserved as dedupe markers because
    only the primary prompt is re-mirrored as a visible harness row.
    """
    normalized_ids: list[str] = []
    seen: set[str] = set()
    for raw_execution_id in execution_ids:
        execution_id = str(raw_execution_id or "").strip()
        if not execution_id or execution_id in seen:
            continue
        seen.add(execution_id)
        normalized_ids.append(execution_id)
    if not session_id or not normalized_ids:
        return 0

    from sqlalchemy import select
    from storage import message_deliveries
    from storage.models import agent_runs

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        reserve_write_lock(conn)
        bindings = {
            str(row["id"]): str(row["delivery_id"])
            for row in
            conn.execute(
                select(agent_runs.c.id, agent_runs.c.delivery_id)
                .where(agent_runs.c.id.in_(normalized_ids))
                .where(agent_runs.c.session_id == session_id)
                .where(agent_runs.c.delivery_id.is_not(None))
            ).mappings()
        }
        retired = 0
        for run_id, delivery_id in bindings.items():
            delivery = message_deliveries.get_delivery(conn, str(delivery_id))
            if delivery is None or delivery["state"] != "queued":
                continue
            if run_id == resubmit_execution_id:
                retired += int(
                    message_deliveries.retire_queued_for_resubmission(
                        conn,
                        session_id,
                        str(delivery_id),
                        expected_dedupe_key=f"avibe:agent_run:{resubmit_execution_id}",
                    )
                )
            else:
                retired += int(
                    message_deliveries.retire_queued(conn, session_id, str(delivery_id))
                )
        return retired


def _serialize_task_mirror(
    method: Callable[..., _TaskStoreResult],
) -> Callable[..., _TaskStoreResult]:
    """Hold one mirror generation across each read-modify-write operation."""

    @wraps(method)
    def _locked(
        self: "ScheduledTaskStore",
        *args: Any,
        **kwargs: Any,
    ) -> _TaskStoreResult:
        with self._reload_lock:
            return method(self, *args, **kwargs)

    return _locked


class ScheduledTaskStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or (paths.get_state_dir() / "scheduled_tasks.json")
        self._sqlite = SQLiteBackgroundTaskStore() if path is None else None
        self._signature: Optional[_PathSignature] = None
        self._tasks: Dict[str, ScheduledTask] = {}
        #: Set when a failed write left this mirror INCOMPLETE, cleared by the reload
        #: that repairs it. See ``maybe_reload`` and ``_reload_after_lost_write``.
        self._reload_required = False
        self._reload_lock = threading.RLock()
        self.load()

    def load(self) -> None:
        with self._reload_lock:
            self._load_unlocked()

    def _load_unlocked(self) -> None:
        if self._sqlite is not None:
            self._tasks = {
                item["id"]: ScheduledTask.from_dict(item)
                for item in self._sqlite.list_scheduled_tasks()
            }
            self._reload_required = False
            return
        if not self.path.exists():
            self._tasks = {}
            self._signature = None
            self._reload_required = False
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("Failed to load scheduled tasks: %s", exc)
            self._tasks = {}
            self._signature = None
            return

        raw_tasks = payload.get("tasks", []) if isinstance(payload, dict) else []
        tasks: Dict[str, ScheduledTask] = {}
        for item in raw_tasks:
            if not isinstance(item, dict):
                continue
            task = ScheduledTask.from_dict(item)
            tasks[task.id] = task
        self._tasks = tasks
        self._signature = _path_signature(self.path)
        self._reload_required = False

    def maybe_reload(self) -> bool:
        """Refresh the mirror when the database changed -- or when WE know it is stale.

        HFR-277, the watch store's twin. ``PRAGMA data_version`` (the probe behind
        ``self._sqlite.maybe_reload``) only reports a COMMITTED write by another
        connection, and the write that drops an entry in ``_reload_after_lost_write``
        ROLLED BACK: data_version is unchanged, so every later call here saw "nothing
        changed" and the dropped definition stayed invisible in-process while its row sat
        enabled in SQLite. That is durable until a restart or an unrelated commit happens
        to bump the counter -- and ``reconcile_jobs`` schedules out of exactly this dict,
        so the task simply never fires again.

        ``_reload_required`` is the fix: an in-process flag, not a database column,
        because the state it records is a property of THIS mirror, not of the data. A
        restart reloads from SQLite anyway, so there is nothing for it to survive; making
        it durable would mean writing to the very database that was just proven
        unwritable. It is cleared only by the reload that repairs the mirror (``load``),
        so a reload that fails again keeps retrying on every later tick.
        """

        with self._reload_lock:
            return self._maybe_reload_unlocked()

    def _maybe_reload_unlocked(self) -> bool:
        if self._sqlite is not None:
            changed = self._sqlite.maybe_reload()
            if self._reload_required:
                try:
                    self.load()
                except Exception:
                    # Still unreachable. Keep the flag and the incomplete mirror, and
                    # report "nothing changed" -- the retry is the next tick's.
                    logger.exception(
                        "Could not reload scheduled tasks after a lost write; the live "
                        "store stays incomplete until a later attempt succeeds"
                    )
                    return False
                return True
            if changed:
                self.load()
            return changed
        signature = _path_signature(self.path)
        if signature == self._signature and not self._reload_required:
            return False
        self.load()
        return True

    def _save(self) -> None:
        if self._sqlite is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"tasks": [task.to_dict() for task in self.list_tasks()]}
        write_atomic(self.path, json.dumps(payload, indent=2))
        self._signature = _path_signature(self.path)

    def list_tasks(self) -> list[ScheduledTask]:
        with self._reload_lock:
            return sorted(
                self._tasks.values(),
                key=lambda item: (item.created_at, item.id),
            )

    def get_task(self, task_id: str) -> Optional[ScheduledTask]:
        with self._reload_lock:
            return self._tasks.get(task_id)

    def refresh_task(self, task_id: str) -> Optional[ScheduledTask]:
        """Reload and read one scheduler definition under the same mirror lock."""

        with self._reload_lock:
            self._maybe_reload_unlocked()
            task = self._tasks.get(task_id)
            return ScheduledTask.from_dict(task.to_dict()) if task is not None else None

    def get_watch_definition(self, definition_id: str) -> Optional[Dict[str, Any]]:
        """The watch row for *definition_id*, or ``None`` when it is not a watch.

        ``get_task`` mirrors ``scheduled_task`` rows only, so a WATCH looked up
        through it reads as "no definition at all": no name, and no signal that
        ``vibe task …`` is the wrong vocabulary for it. Watches and tasks are both
        ``run_definitions`` rows and this store already holds the backend that reads
        them, so a caller that must describe whichever definition a run belongs to
        (the owed-failure-notice copy) can resolve either one without a second store.

        ``None`` on the file backend, which has no definition table to read.
        """

        identifier = str(definition_id or "").strip()
        if not identifier or self._sqlite is None:
            return None
        return self._sqlite.get_watch(identifier)

    @staticmethod
    def _read_state(task: ScheduledTask) -> DefinitionWriteExpectation:
        """The guarded state a full-row payload for ``task`` is derived from.

        Called BEFORE the mutation, because the in-memory task IS the read: every
        caller here loaded it from SQLite (``load`` / ``maybe_reload``), edits a few
        fields, and then writes every column back. ``deleted_at`` is not a field of
        ``ScheduledTask`` at all -- the store only ever lists live rows -- which is
        why the full-row write would otherwise resurrect a removed task.
        """

        return DefinitionWriteExpectation.from_read(
            session_id=task.session_id,
            enabled=task.enabled,
            deleted_at=None,
            metadata=task.metadata,
        )

    @property
    def sqlite_backend(self) -> Optional[SQLiteBackgroundTaskStore]:
        """The SQLite backend behind this store, or ``None`` for the file backend.

        The guard ``_write_task`` applies lives in SQLite; the file backend has no
        compare-and-set at all. Exposed so a caller can ask whether a guarded stamp and
        an outbox row can be committed together (HFR-269).
        """

        return self._sqlite

    def _write_task(
        self,
        task: ScheduledTask,
        expect: DefinitionWriteExpectation,
        *,
        queued_run: Optional[dict[str, Any]] = None,
        expected_uncanceled_run_id: Optional[str] = None,
        expected_enabled_agent_id: Optional[str] = None,
        expected_reference_agent_id: Optional[str] = None,
        expected_schedule_generation: Optional[dict[str, str]] = None,
        expected_terminal_run_id: Optional[str] = None,
    ) -> bool:
        """Persist a whole task row; ``False`` means the guard refused the write.

        ``queued_run`` is an ``agent_runs`` outbox payload this write AUTHORISES; it is
        committed in the SAME transaction as the row (HFR-269), so a refusal or a
        failure leaves neither behind. Only the SQLite backend can do that, and passing
        one to the file backend is a caller bug -- see ``sqlite_backend``.

        On refusal the in-memory mirror is reloaded, so the store never keeps serving
        the mutated task that the database rejected -- and on a RAISED write too
        (HFR-272, the watch store's twin). A rolled-back transaction and a refused one
        leave the database in exactly the same place; only the ``False`` return was ever
        being handled, so a disk error or a locked database left every in-process reader
        (``reconcile_jobs`` schedules from this dict, ``_read_state`` derives the next
        compare-and-set's expectation from it) serving an edit that does not exist.
        """

        try:
            if self._sqlite is None:
                # The watch store's answer, replicated verbatim: REFUSE rather than
                # save-then-enqueue. A file-backed store cannot make the two writes one
                # decision at all, and a best-effort second write is exactly the
                # two-commits bug HFR-269 removed.
                if queued_run is not None:
                    raise ValueError(
                        "a file-backed scheduled task store cannot commit a queued run "
                        "with the task row"
                    )
                self._save()
                _publish_task_definitions_updated()
                return True
            if queued_run is None:
                landed = self._sqlite.upsert_scheduled_task(
                    task.to_dict(),
                    expect=expect,
                    expected_enabled_agent_id=expected_enabled_agent_id,
                    expected_reference_agent_id=expected_reference_agent_id,
                    expected_schedule_generation=expected_schedule_generation,
                    expected_terminal_run_id=expected_terminal_run_id,
                )
            else:
                # No agent-guard kwargs: the only caller that passes a queued run is
                # ``mark_task_result``, which never passes them either.
                landed = self._sqlite.upsert_scheduled_task_with_queued_run(
                    task.to_dict(),
                    expect=expect,
                    run_payload=queued_run,
                    expected_uncanceled_run_id=expected_uncanceled_run_id,
                    expected_schedule_generation=expected_schedule_generation,
                    expected_terminal_run_id=expected_terminal_run_id,
                )
        except Exception:
            self._reload_after_lost_write(task.id)
            raise
        if landed:
            _publish_task_definitions_updated()
            return True
        self.load()
        return False

    def _reload_after_lost_write(self, task_id: str) -> None:
        """Drop a mirror entry the database did not accept, reloading if it can.

        The watch store's twin, and for the same reason: the reload can fail too, and a
        missing definition is a safer thing for the scheduler to act on than a mutated
        one that was never stored. ``maybe_reload`` restores it once the database is
        reachable again -- which it can only do because dropping the entry also marks
        the mirror as needing an UNCONDITIONAL reload (HFR-277). The failed write rolled
        back, so ``PRAGMA data_version`` never moved and the probe alone would report
        "nothing changed" forever.
        """

        try:
            self.load()
        except Exception:
            logger.exception(
                "Could not reload scheduled tasks after a failed write; dropping the "
                "stale mirror entry for %s",
                task_id,
            )
            self._tasks.pop(task_id, None)
            self._signature = None
            self._reload_required = True

    @_serialize_task_mirror
    def upsert_task(
        self,
        task: ScheduledTask,
        *,
        expected_enabled_agent_id: Optional[str] = None,
        expected_reference_agent_id: Optional[str] = None,
    ) -> ScheduledTask:
        """Create or adopt a whole task row (unguarded: the payload is not a re-read).

        The mirror rolls back with the write here too (HFR-275, the watch store's twin).
        This is the one entry point that can add an id the database has never seen, and a
        phantom is worse than a stale edit: ``reconcile_jobs`` would SCHEDULE a task whose
        creation the caller was told had FAILED and fire its prompt into a channel, with
        no durable row to stop it and nothing to reload it away.
        """

        task.updated_at = _utc_now_iso()
        self._tasks[task.id] = task
        try:
            if self._sqlite is not None:
                # No ``expect``: this is the create/adopt entry point (``add_task``),
                # where the payload is not derived from a stored row.
                self._sqlite.upsert_scheduled_task(
                    task.to_dict(),
                    expected_enabled_agent_id=expected_enabled_agent_id,
                    expected_reference_agent_id=expected_reference_agent_id,
                )
                if expected_reference_agent_id is not None:
                    self.load()
                    _publish_task_definitions_updated()
                    return self._tasks[task.id]
                _publish_task_definitions_updated()
                return task
            self._save()
        except Exception:
            self._reload_after_lost_write(task.id)
            raise
        _publish_task_definitions_updated()
        return task

    def add_task(
        self,
        *,
        name: Optional[str] = None,
        session_key: str,
        session_id: Optional[str] = None,
        prompt: str,
        schedule_type: str,
        agent_name: Optional[str] = None,
        session_policy: Optional[str] = None,
        post_to: Optional[str] = None,
        deliver_key: Optional[str] = None,
        cwd: Optional[str] = None,
        cron: Optional[str] = None,
        run_at: Optional[str] = None,
        timezone_name: str,
        shell_command: Optional[str] = None,
        command: Optional[list[str]] = None,
        timeout_seconds: Optional[float] = None,
        metadata: Optional[dict[str, Any]] = None,
        expected_enabled_agent_id: Optional[str] = None,
        expected_reference_agent_id: Optional[str] = None,
        user_context: Any = None,
    ) -> ScheduledTask:
        from core.vibe_agents import ensure_agent_name_access
        from storage.resource_access_service import (
            ensure_harness_definition_write,
            metadata_with_resource_user_context,
        )

        ensure_harness_definition_write(user_context)
        ensure_agent_name_access(agent_name, user_context=user_context)
        task = ScheduledTask(
            id=uuid4().hex[:12],
            name=name,
            session_key=session_key,
            session_id=session_id,
            prompt=prompt,
            schedule_type=schedule_type,
            agent_name=agent_name,
            session_policy=session_policy or ("existing" if session_id or session_key else None),
            post_to=post_to,
            deliver_key=deliver_key,
            cwd=cwd,
            cron=cron,
            run_at=run_at,
            timezone=timezone_name,
            metadata=metadata_with_resource_user_context(metadata, user_context),
            shell_command=shell_command,
            command=command,
            timeout_seconds=timeout_seconds,
        )
        return self.upsert_task(
            task,
            expected_enabled_agent_id=expected_enabled_agent_id,
            expected_reference_agent_id=expected_reference_agent_id,
        )

    @_serialize_task_mirror
    def remove_task(self, task_id: str) -> bool:
        """Delete a task; the mirror rolls back with the delete (HFR-275).

        The safer direction of the same class -- an entry dropped here reads as "gone"
        and stops the schedule -- but silently, and NOT self-healing: with the row still
        there and unchanged, ``maybe_reload`` sees no external write, so the task the user
        was told could not be deleted just stops firing until the process restarts.
        """

        if task_id not in self._tasks:
            return False
        del self._tasks[task_id]
        try:
            if self._sqlite is not None:
                self._sqlite.remove_task(task_id)
                _publish_task_definitions_updated()
                return True
            self._save()
        except Exception:
            self._reload_after_lost_write(task_id)
            raise
        _publish_task_definitions_updated()
        return True

    @_serialize_task_mirror
    def set_enabled(self, task_id: str, enabled: bool) -> ScheduledTask:
        task = self._tasks[task_id]
        if enabled and not task.enabled:
            require_task_resumable(
                task_id,
                metadata=task.metadata,
                session_id=task.session_id,
                schedule_type=task.schedule_type,
                retired_at=task.retired_at,
            )
        expect = self._read_state(task)
        task.enabled = enabled
        task.updated_at = _utc_now_iso()
        if not self._write_task(task, expect):
            # A pause/resume that lost to a teardown must not be reported as applied:
            # this write also restores ``last_error`` and ``session_id`` from the stale
            # mirror, so letting it "succeed" would erase the reclaim's pause reason.
            raise DefinitionWriteConflict(task_id, definition_type="scheduled task")
        return task

    @_serialize_task_mirror
    def update_task(
        self,
        task_id: str,
        *,
        name: Optional[str],
        session_key: str,
        prompt: str,
        schedule_type: str,
        post_to: Optional[str],
        deliver_key: Optional[str],
        cron: Optional[str],
        run_at: Optional[str],
        timezone_name: str,
        session_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        session_policy: Optional[str] = None,
        cwd: Optional[str] = None,
        update_cwd: bool = False,
        shell_command: Optional[str] = None,
        command: Optional[list[str]] = None,
        timeout_seconds: Optional[float] = None,
        update_command_fields: bool = False,
        metadata: Optional[dict[str, Any]] = None,
        expected_enabled_agent_id: Optional[str] = None,
        expected_reference_agent_id: Optional[str] = None,
        user_context: Any = None,
    ) -> ScheduledTask:
        from core.vibe_agents import ensure_agent_name_access
        from storage.resource_access_service import (
            ensure_harness_definition_write,
            metadata_with_resource_user_context,
        )

        ensure_harness_definition_write(user_context)
        ensure_agent_name_access(agent_name, user_context=user_context)
        task = self._tasks[task_id]
        # Captured before the first mutation: this is the state the CALLER read
        # (``vibe task update`` resolved Agents and Sessions from this very object),
        # and it is what the write below re-asserts.
        expect = self._read_state(task)
        previous_schedule = (task.schedule_type, task.run_at, task.timezone)
        task.name = name
        task.session_key = session_key
        task.session_id = session_id
        task.prompt = prompt
        task.schedule_type = schedule_type
        task.agent_name = agent_name
        if session_policy is None:
            session_policy = task.session_policy or ("existing" if session_id or session_key else None)
        task.session_policy = session_policy
        task.post_to = post_to
        task.deliver_key = deliver_key
        if update_cwd:
            task.cwd = cwd
        task.cron = cron
        task.run_at = run_at
        task.timezone = timezone_name
        # Changing a one-shot schedule creates a new lifecycle. The previous
        # terminal fact remains in Run history but cannot own the new instant.
        if (schedule_type, run_at, timezone_name) != previous_schedule:
            task.retired_at = None
            task.retirement_reason = None
            task.last_run_id = None
        if update_command_fields:
            # Gated like ``cwd``: an edit that says nothing about the command must not
            # clear it, and ``last_exit_code`` is runtime state no edit ever rewrites.
            task.shell_command = shell_command
            task.command = command
            task.timeout_seconds = timeout_seconds
        task.metadata = metadata_with_resource_user_context(
            metadata if metadata is not None else task.metadata,
            user_context,
        )
        task.updated_at = _utc_now_iso()
        if not self._write_task(
            task,
            expect,
            expected_enabled_agent_id=expected_enabled_agent_id,
            expected_reference_agent_id=expected_reference_agent_id,
        ):
            # The edit did NOT land, and its payload would have restored the Session
            # binding, enabled state and reclaim snapshot the teardown just changed.
            # Raising is the contract: ``cmd_task_update`` prints an error and exits
            # non-zero instead of echoing a task the database never accepted.
            raise DefinitionWriteConflict(task_id, definition_type="scheduled task")
        if expected_reference_agent_id is not None:
            self.load()
            return self._tasks[task_id]
        return task

    @_serialize_task_mirror
    def retire_missed_one_shot(
        self,
        task_id: str,
        *,
        expected_run_at: str,
        expected_timezone: str,
        expected_updated_at: str,
    ) -> bool:
        """Retire the exact one-shot schedule APScheduler reported missed."""

        task = self._tasks.get(task_id)
        if (
            task is None
            or not task.enabled
            or task.schedule_type != "at"
            or task.retired_at is not None
            or task.run_at != expected_run_at
            or task.timezone != expected_timezone
            or task.updated_at != expected_updated_at
        ):
            return False
        if self._sqlite is not None:
            landed = self._sqlite.retire_missed_one_shot(
                task_id,
                expected_run_at=expected_run_at,
                expected_timezone=expected_timezone,
                expected_updated_at=expected_updated_at,
            )
            if landed:
                self.load()
            return landed
        task.enabled = False
        task.retired_at = _utc_now_iso()
        task.retirement_reason = TASK_RETIREMENT_SCHEDULE_MISSED
        task.last_run_at = None
        task.last_run_id = None
        task.last_error = None
        task.last_exit_code = None
        task.metadata.pop(COMMAND_TIMED_OUT_METADATA_KEY, None)
        task.metadata.pop(TASK_LAST_RESULT_STATUS_METADATA_KEY, None)
        task.updated_at = task.retired_at
        self._save()
        return True

    @_serialize_task_mirror
    def record_binding_recovery(
        self,
        task_id: str,
        payload: dict[str, Any],
        *,
        binding_notice: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Durably stamp what was done about a broken session binding.

        Written through the store (not by mutating a task the next
        ``maybe_reload`` may replace) because it is what makes the notification
        once-per-transition instead of once-per-fire. When a successful rebind
        needs its own notice, both rows commit in one SQLite transaction: a failed
        notice write therefore cannot leave a marker that suppresses every retry.
        """

        self.maybe_reload()
        task = self._tasks.get(task_id)
        if task is None:
            return False
        expect = self._read_state(task)
        metadata = dict(task.metadata or {})
        metadata[BINDING_RECOVERY_METADATA_KEY] = dict(payload)
        task.metadata = metadata
        task.updated_at = _utc_now_iso()
        # A runtime stamp, not a user action: a lost write is reported by the return
        # value (the caller already treats ``False`` as "nothing recorded") rather than
        # by an exception through the fire path.
        if self._sqlite is not None and binding_notice is not None:
            try:
                landed = self._sqlite.upsert_scheduled_task_with_binding_notice(
                    task.to_dict(),
                    expect=expect,
                    notice=binding_notice,
                )
            except Exception:
                self._reload_after_lost_write(task.id)
                raise
            if landed:
                return True
            self.load()
            return False
        return self._write_task(task, expect)

    @_serialize_task_mirror
    def list_orphaned_reservations(self, task_id: str) -> list[dict[str, Any]]:
        """The reserved sessions recorded against ``task_id`` that were never given back."""

        task = self._tasks.get(task_id)
        if task is None or not isinstance(task.metadata, dict):
            return []
        entries = task.metadata.get(ORPHANED_RESERVATIONS_METADATA_KEY)
        if not isinstance(entries, list):
            return []
        return [dict(entry) for entry in entries if isinstance(entry, dict)]

    @_serialize_task_mirror
    def record_orphaned_reservations(self, task_id: str, entries: list[dict[str, Any]]) -> bool:
        """Durably record (or clear) the reservations this definition could not release.

        HFR-276. Written through the store, from a FRESH read, for the same reason
        ``record_binding_recovery`` is: the caller reaches here on a path where the
        previous full-row write was already refused, so the mirror it holds is stale
        and ``_write_task`` has since reloaded the cache. Deriving the expectation from
        the reloaded row is what makes this write land in the very race that produced
        the orphan, instead of being refused by the same teardown twice.

        ``False`` means nothing was recorded -- the definition was removed, or a second
        teardown refused this write too -- and the caller MUST consume it: the whole
        point of the record is that the id is otherwise unrecoverable, so silently
        losing it is the same defect one layer further in.
        """

        self.maybe_reload()
        task = self._tasks.get(task_id)
        if task is None:
            return False
        expect = self._read_state(task)
        metadata = dict(task.metadata or {})
        if entries:
            metadata[ORPHANED_RESERVATIONS_METADATA_KEY] = [dict(entry) for entry in entries]
        else:
            metadata.pop(ORPHANED_RESERVATIONS_METADATA_KEY, None)
        task.metadata = metadata
        task.updated_at = _utc_now_iso()
        return self._write_task(task, expect)

    @_serialize_task_mirror
    def mark_task_result(
        self,
        task_id: str,
        *,
        error: Optional[str],
        disable_one_shot: bool = True,
        expected_binding: Optional[tuple[Optional[str], str, str]] = None,
        exit_code: Optional[int] = None,
        records_command_outcome: bool = False,
        timed_out: bool = False,
        result_status: Optional[str] = None,
        queued_run: Optional[dict[str, Any]] = None,
        expected_uncanceled_run_id: Optional[str] = None,
        expected_schedule_generation: Optional[dict[str, str]] = None,
        expected_terminal_run_id: Optional[str] = None,
        expected_non_owner_retired_one_shot: Optional[
            DefinitionWriteExpectation
        ] = None,
    ) -> bool:
        """Stamp a fire's outcome; ``False`` means the store refused the write.

        ``queued_run`` is the outbox row this stamp AUTHORISES -- today the
        ``--on-failure agent`` escalation turn. Passing it makes the stamp and the turn
        ONE transaction (HFR-269): both land or neither does, so no teardown can commit
        between them and a failed one-shot ``at`` task cannot be disabled while losing
        the report that explains why.

        ``records_command_outcome`` marks the ONE stamp that is a command fire's own
        result, and it is what makes ``exit_code`` authoritative in both directions --
        see the write below.

        ``result_status`` is the existing terminal Run verdict when this stamp is a
        projection from that ledger. It is stored in definition metadata so read
        surfaces never have to recover cancellation from localized ``error`` text.

        ``expected_uncanceled_run_id`` re-asserts, inside that same transaction, that
        the fire being stamped has not been stopped -- see
        ``upsert_scheduled_task_with_queued_run``. Only meaningful alongside
        ``queued_run``, because it exists to stop a stopped run queuing an Agent turn.

        ``expected_non_owner_retired_one_shot`` is the binding authority captured
        before a marker-less manual or legacy Run started. Its presence means the Run
        owns only its history and optional escalation outbox row, even if the user has
        replaced the retired schedule before this result arrives.
        """

        self.maybe_reload()
        task = self._tasks.get(task_id)
        if task is None:
            return False
        non_owner_expectation = expected_non_owner_retired_one_shot
        if non_owner_expectation is None and (
            expected_schedule_generation is None
            and task.schedule_type == "at"
            and task.retired_at is not None
        ):
            non_owner_expectation = self._read_state(task)
        if non_owner_expectation is not None:
            # A manual or legacy Run can still settle after a one-shot retires, but
            # without the immutable consumed-generation marker it owns only its Run
            # history. Keep every terminal definition fact byte-for-byte unchanged.
            if queued_run is None:
                return True
            if self._sqlite is None:
                raise ValueError(
                    "a file-backed scheduled task store cannot atomically enqueue "
                    "a task escalation"
                )
            try:
                landed = self._sqlite.enqueue_task_escalation_without_definition_write(
                    task.id,
                    expect=non_owner_expectation,
                    expected_session_key=(
                        expected_binding[1]
                        if expected_binding is not None
                        else task.session_key
                    ),
                    run_payload=queued_run,
                    expected_uncanceled_run_id=str(expected_uncanceled_run_id or ""),
                )
            finally:
                # A replacement may have committed after maybe_reload() above. The
                # caller reconciles physical jobs immediately after this result.
                self.load()
            return landed
        if expected_binding is not None and (
            task.session_id,
            task.session_key,
            task.schedule_type,
        ) != expected_binding:
            return False
        if expected_schedule_generation is not None:
            if (
                expected_terminal_run_id is None
                or task.schedule_type != "at"
                or task.enabled
                or task.run_at != expected_schedule_generation["run_at"]
                or task.timezone != expected_schedule_generation["timezone"]
                or task.retired_at != expected_schedule_generation["retired_at"]
                or task.retirement_reason != TASK_RETIREMENT_SCHEDULE_CONSUMED
                or task.last_run_id != expected_terminal_run_id
            ):
                return False
        expect = self._read_state(task)
        task.last_run_at = _utc_now_iso()
        task.last_error = error
        normalized_result_status = str(result_status or "").strip()
        if normalized_result_status in TERMINAL_RUN_STATUSES:
            task.metadata = {
                **(task.metadata if isinstance(task.metadata, dict) else {}),
                TASK_LAST_RESULT_STATUS_METADATA_KEY: normalized_result_status,
            }
        elif (
            isinstance(task.metadata, dict)
            and TASK_LAST_RESULT_STATUS_METADATA_KEY in task.metadata
        ):
            task.metadata = dict(task.metadata)
            task.metadata.pop(TASK_LAST_RESULT_STATUS_METADATA_KEY, None)
        if records_command_outcome:
            # A COMMAND FIRE'S OWN STAMP OWNS THIS COLUMN, ``None`` included. A fire
            # that never reached a process -- a working directory that vanished, a
            # supervisor that died during startup -- has no status of its own, and
            # leaving the previous fire's code on the row made every surface report
            # "exited 7" beside a failure that ran no command. The notice already
            # refuses to print an exit code it does not have; the row must refuse to
            # keep one it no longer has.
            task.last_exit_code = int(exit_code) if exit_code is not None else None
            # AND WHETHER THE SCHEDULER IS WHY IT STOPPED, which the code cannot say on
            # its own: the runner reports its own timeout as 124, and a command that
            # wraps itself in ``timeout`` reports a real one as 124 too. Written on
            # every command stamp, both values, so a fire that did NOT time out
            # positively clears the previous fire's claim that one did.
            task.metadata = {
                **(task.metadata if isinstance(task.metadata, dict) else {}),
                COMMAND_TIMED_OUT_METADATA_KEY: bool(timed_out),
            }
        elif exit_code is not None:
            # A stamp from any other lane reports the code only when it has one: a
            # message task has none and must never blank what a command fire of the
            # same definition recorded.
            task.last_exit_code = int(exit_code)
        # SQLite one-shots retire in the scheduler enqueue transaction, tied to
        # the exact run_at/timezone that was consumed. A result arriving after
        # the user replaces and resumes that schedule must not disable the new
        # lifecycle. The legacy file backend has no atomic enqueue boundary, so
        # it retains its result-time transition for compatibility.
        if disable_one_shot and task.schedule_type == "at" and self._sqlite is None:
            task.enabled = False
            if task.retired_at is None:
                task.retired_at = _utc_now_iso()
                task.retirement_reason = TASK_RETIREMENT_SCHEDULE_CONSUMED
        task.updated_at = _utc_now_iso()
        # Same reasoning as ``record_binding_recovery``, and the same reason it must be
        # guarded: this payload carries the mirror's ``session_id`` and ``enabled``, so
        # a run result landing after a ``/new`` reclaim would re-enable the definition
        # and re-point it at the session the reclaim just tore down.
        return self._write_task(
            task,
            expect,
            queued_run=queued_run,
            expected_uncanceled_run_id=expected_uncanceled_run_id,
            expected_schedule_generation=expected_schedule_generation,
            expected_terminal_run_id=expected_terminal_run_id,
        )

    def suspend_task(self, task_id: str, *, error: str) -> bool:
        """Atomically disable a definition before an unsafe execution boundary."""

        self.maybe_reload()
        task = self._tasks.get(task_id)
        if task is None:
            return False
        expect = self._read_state(task)
        task.enabled = False
        task.last_run_at = _utc_now_iso()
        task.last_error = error
        task.updated_at = _utc_now_iso()
        return self._write_task(task, expect)


class TaskExecutionStore:
    def __init__(self, root: Optional[Path] = None):
        self.root = root or (paths.get_state_dir() / "task_requests")
        self._sqlite = SQLiteBackgroundTaskStore() if root is None else None
        self.pending_dir = self.root / "pending"
        self.processing_dir = self.root / "processing"
        self.completed_dir = self.root / "completed"
        self._file_enqueue_lock = threading.Lock()
        self._ensure_dirs()
        self._signature = self._state_signature()

    @property
    def sqlite_backend(self) -> Optional[SQLiteBackgroundTaskStore]:
        """The SQLite backend behind this store, or ``None`` for the file backend.

        Exposed so a caller that must commit an outbox row TOGETHER with another
        write can find out whether the two live in one database (HFR-269). The file
        backend keeps runs in a directory of JSON files and can share a transaction
        with nothing.
        """

        return self._sqlite

    def _ensure_dirs(self) -> None:
        if self._sqlite is not None:
            return
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.processing_dir.mkdir(parents=True, exist_ok=True)
        self.completed_dir.mkdir(parents=True, exist_ok=True)

    def _state_signature(self) -> tuple[_DirectoryEntrySignature, ...] | None:
        if self._sqlite is not None:
            return None
        return (
            _directory_entry_signature(self.pending_dir),
            _directory_entry_signature(self.processing_dir),
            _directory_entry_signature(self.completed_dir),
        )

    def maybe_reload(self) -> bool:
        if self._sqlite is not None:
            return self._sqlite.maybe_reload()
        signature = self._state_signature()
        if signature == self._signature:
            return False
        self._signature = signature
        return True

    def _request_path(self, request_id: str, *, state: str) -> Path:
        directory = {
            "pending": self.pending_dir,
            "processing": self.processing_dir,
            "completed": self.completed_dir,
        }[state]
        return directory / f"{request_id}.json"

    def recover_processing(
        self,
        *,
        interruption_error: Optional[str] = None,
        interrupt_reason: str = SETTLED_BY_RESTARTED,
        cancellation_error: Optional[str] = None,
    ) -> None:
        """Settle the fires a dead service left ``running``.

        A fire that still carries a ``command_worker`` record is left ``running``:
        that record is the last name anyone has for a process this start could not
        prove dead, and it is readable only while the row is ``running``. Settling it
        would leave a backup or deployment running with nothing able to find it, so
        the row waits for the next start to retry -- bounded by
        ``_retain_unreaped_command_worker``, which stops re-stamping after
        ``_MAX_COMMAND_WORKER_REAP_ATTEMPTS``.
        """

        interruption_error = interruption_error or i18n_t(
            SETTLEMENT_I18N_KEYS[interrupt_reason],
            "en",
        )
        cancellation_error = cancellation_error or i18n_t(
            SETTLEMENT_I18N_KEYS[SETTLED_BY_STOPPED],
            "en",
        )
        if self._sqlite is not None:
            self._sqlite.recover_processing_runs(
                interruption_error=interruption_error,
                interrupt_reason=interrupt_reason,
                cancellation_error=cancellation_error,
            )
            return
        self._ensure_dirs()
        for path in self.processing_dir.glob("*.json"):
            pending_path = self.pending_dir / path.name
            completed_path = self.completed_dir / path.name
            if pending_path.exists():
                path.unlink(missing_ok=True)
                continue
            if completed_path.exists():
                path.unlink(missing_ok=True)
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
            stored_metadata = payload.get("metadata") if isinstance(payload, dict) else None
            if isinstance(stored_metadata, dict) and stored_metadata.get(
                COMMAND_WORKER_METADATA_KEY
            ):
                # Same rule as the SQLite pass: a surviving worker record means a
                # process this start could not prove dead, and this file is the only
                # place its identity is written. Requeuing is no safer than settling --
                # it would run the command again alongside the one still running.
                continue
            if not isinstance(payload, dict) or not payload.get("pid"):
                path.replace(pending_path)
                continue
            self._settle_processing_file(
                path,
                payload,
                interruption_error=interruption_error,
                cancellation_error=cancellation_error,
                interrupt_reason=interrupt_reason,
            )

    def _settle_processing_file(
        self,
        path: Path,
        payload: dict[str, Any],
        *,
        interruption_error: str,
        cancellation_error: str,
        interrupt_reason: str,
    ) -> str:
        """Write one processing file out to ``completed`` as a result-less terminal.

        The file store's only settlement primitive, shared by the startup pass above and
        the per-run settle below so that both write the same row. A cancel the user
        already requested wins the status and the reason: they asked before the service
        could answer, and the answer never came.
        """

        now = _utc_now_iso()
        metadata = payload.get("metadata")
        canceled = bool(payload.get("cancel_requested"))
        status = "canceled" if canceled else "failed"
        payload.update(
            {
                "status": status,
                "ok": False,
                "error": cancellation_error if canceled else interruption_error,
                "completed_at": now,
                "updated_at": now,
                "metadata": {
                    **(metadata if isinstance(metadata, dict) else {}),
                    "interrupt_reason": SETTLED_BY_STOPPED if canceled else interrupt_reason,
                },
            }
        )
        write_atomic(self.completed_dir / path.name, json.dumps(payload, indent=2))
        path.unlink(missing_ok=True)
        return status

    def settle_recovered_run(
        self,
        run_id: str,
        *,
        interruption_error: str,
        cancellation_error: str,
        interrupt_reason: str = SETTLED_BY_RESTARTED,
    ) -> Optional[str]:
        """Terminalize the ONE run whose retained worker has just been proven dead.

        ``recover_processing`` settles a whole start's worth of rows and deliberately
        steps over any row that still names a worker; this settles the single row whose
        worker was proven dead later, mid-life, once closing it can no longer orphan
        anything. Both backends answer, because the leak is the same on both: a run left
        ``running`` for a command that is long over, until some later start notices.

        Returns the status written, or ``None`` when nothing was: the SQLite write is
        status-scoped, and on the file store the processing file may already be gone.
        """

        if self._sqlite is not None:
            return self.settle_without_result(
                run_id,
                terminal_status=SETTLEMENT_TERMINAL_STATUS.get(interrupt_reason, "failed"),
                error=interruption_error,
                metadata={"interrupt_reason": interrupt_reason},
            )
        processing_path = self._request_path(run_id, state="processing")
        try:
            payload = json.loads(processing_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        return self._settle_processing_file(
            processing_path,
            payload,
            interruption_error=interruption_error,
            cancellation_error=cancellation_error,
            interrupt_reason=interrupt_reason,
        )

    def mark_execution_started(self, request_id: str) -> bool:
        """Persist the boundary between a bare claim and executing its coroutine."""

        now = _utc_now_iso()
        if self._sqlite is not None:
            return self._sqlite.mark_run_execution_started(
                request_id,
                pid=os.getpid(),
                updated_at=now,
            )
        processing_path = self._request_path(request_id, state="processing")
        if not processing_path.exists():
            return False
        try:
            payload = json.loads(processing_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not isinstance(payload, dict):
            return False
        payload.update({"pid": os.getpid(), "started_at": now, "updated_at": now})
        write_atomic(processing_path, json.dumps(payload, indent=2))
        return True

    def record_command_worker(
        self,
        request_id: str,
        identity: Optional[dict[str, Any]],
    ) -> bool:
        """Remember (or forget) the isolated supervisor this in-flight fire spawned.

        See ``SQLiteBackgroundTaskStore.record_command_worker``.
        """

        return self._merge_inflight_metadata(
            request_id, COMMAND_WORKER_METADATA_KEY, identity
        )

    def record_command_snapshot(
        self,
        request_id: str,
        snapshot: Optional[dict[str, Any]],
    ) -> bool:
        """Re-stamp what this in-flight fire is about to run.

        See ``SQLiteBackgroundTaskStore.record_command_snapshot``.
        """

        return self._merge_inflight_metadata(
            request_id, COMMAND_SNAPSHOT_METADATA_KEY, snapshot
        )

    def _merge_inflight_metadata(
        self,
        request_id: str,
        key: str,
        value: Optional[dict[str, Any]],
    ) -> bool:
        """Set (or drop) one metadata key on an in-flight fire.

        The file backend keeps the same keys in the processing payload's ``metadata``
        so a store swapped in by a test observes the same contract as SQLite.
        """

        if self._sqlite is not None:
            return self._sqlite.merge_running_run_metadata(request_id, key, value)
        processing_path = self._request_path(request_id, state="processing")
        try:
            payload = json.loads(processing_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not isinstance(payload, dict):
            return False
        metadata = payload.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        if value is None:
            if key not in metadata:
                return False
            metadata.pop(key, None)
        else:
            metadata[key] = dict(value)
        payload["metadata"] = metadata
        payload["updated_at"] = _utc_now_iso()
        write_atomic(processing_path, json.dumps(payload, indent=2))
        return True

    def list_running_command_workers(self) -> list[dict[str, Any]]:
        """Every in-flight fire that still names a command worker.

        ``definition_id`` is part of the contract, not decoration: a caller asking
        whether ITS definition still has a worker running filters on it, so a backend
        that omits the field does not report "no worker" -- it reports it about every
        definition at once, and single flight silently stops holding here. The file
        payload spells the same association ``task_id`` (SQLite renamed that column),
        so the field is normalized at this boundary rather than at each reader.
        """

        if self._sqlite is not None:
            return self._sqlite.list_running_command_workers()
        self._ensure_dirs()
        workers: list[dict[str, Any]] = []
        for path in sorted(self.processing_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            metadata = payload.get("metadata")
            identity = (
                metadata.get(COMMAND_WORKER_METADATA_KEY)
                if isinstance(metadata, dict)
                else None
            )
            if isinstance(identity, dict):
                definition_id = payload.get("task_id")
                workers.append(
                    {
                        "run_id": path.stem,
                        "definition_id": str(definition_id) if definition_id else None,
                        "identity": identity,
                    }
                )
        return workers

    @staticmethod
    def queued_run_payload(request: TaskExecutionRequest) -> dict[str, Any]:
        """The ``agent_runs`` payload ``enqueue`` would write for *request*.

        Exposed so a caller that commits the outbox row inside ANOTHER transaction
        (HFR-269) writes exactly the row this store would have written, rather than a
        second, drifting copy of the same mapping.
        """

        payload = request.to_dict()
        payload["status"] = "queued"
        payload["updated_at"] = request.created_at
        return payload

    def enqueue(
        self,
        request: TaskExecutionRequest,
        *,
        expected_enabled_agent_id: Optional[str] = None,
        expected_reference_agent_id: Optional[str] = None,
    ) -> TaskExecutionRequest:
        if self._sqlite is not None:
            self._sqlite.enqueue_run(
                self.queued_run_payload(request),
                expected_enabled_agent_id=expected_enabled_agent_id,
                expected_reference_agent_id=expected_reference_agent_id,
            )
            if expected_reference_agent_id is not None:
                stored = self._sqlite.get_run(request.id)
                if stored is not None:
                    request.agent_id = stored.get("agent_id")
                    request.agent_name = stored.get("agent_name")
            return request
        self._ensure_dirs()
        path = self._request_path(request.id, state="pending")
        write_atomic(path, json.dumps(request.to_dict(), indent=2))
        return request

    def enqueue_task_run(
        self,
        task_id: str,
        *,
        source_kind: str = "cli",
        task: Optional[ScheduledTask] = None,
        suppress_scheduler_successor: bool = False,
        expected_run_at: Optional[str] = None,
        expected_timezone: Optional[str] = None,
        expected_updated_at: Optional[str] = None,
        expected_job_id: Optional[str] = None,
        terminal_error: Optional[str] = None,
    ) -> Optional[TaskExecutionRequest]:
        if task is None:
            request = TaskExecutionRequest(
                id=uuid4().hex[:12],
                request_type="scheduled",
                task_id=task_id,
                source_kind=source_kind,
            )
            if self._sqlite is not None:
                if suppress_scheduler_successor and any(
                    pending.task_id == task_id
                    and pending.source_kind == "scheduler"
                    for pending in self.list_pending()
                ):
                    return None
                return self.enqueue(request)
            with self._file_enqueue_lock:
                if (
                    suppress_scheduler_successor
                    and self._file_has_unstarted_scheduler_run(task_id)
                ):
                    return None
                return self.enqueue(request)
        metadata = dict(task.metadata or {})
        snapshot = command_snapshot(task)
        if snapshot is not None:
            # Taken HERE, where the run row is created, because this is the last moment
            # at which the definition and the work about to run are the same thing. The
            # definition is editable and deletable from this instant on; the run is not.
            metadata[COMMAND_SNAPSHOT_METADATA_KEY] = snapshot
        return self.enqueue_definition_run(
            definition_id=task.id,
            run_type="scheduled",
            source_kind=source_kind,
            session_key=task.session_key,
            session_id=task.session_id,
            post_to=task.post_to,
            deliver_key=task.deliver_key,
            prompt=task.prompt,
            agent_name=task.agent_name,
            session_policy=task.session_policy,
            metadata=metadata,
            suppress_scheduler_successor=suppress_scheduler_successor,
            expected_run_at=expected_run_at,
            expected_timezone=expected_timezone,
            expected_updated_at=(
                expected_updated_at
                if expected_updated_at is not None
                else task.updated_at if expected_run_at is not None else None
            ),
            expected_job_id=expected_job_id,
            terminal_error=terminal_error,
        )

    def enqueue_definition_run(
        self,
        *,
        definition_id: str,
        run_type: str,
        source_kind: str,
        session_key: str,
        session_id: Optional[str],
        post_to: Optional[str],
        deliver_key: Optional[str],
        prompt: str,
        agent_name: Optional[str],
        session_policy: Optional[str],
        source_actor: Optional[str] = None,
        parent_run_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        suppress_scheduler_successor: bool = False,
        expected_run_at: Optional[str] = None,
        expected_timezone: Optional[str] = None,
        expected_updated_at: Optional[str] = None,
        expected_job_id: Optional[str] = None,
        terminal_error: Optional[str] = None,
    ) -> Optional[TaskExecutionRequest]:
        request = TaskExecutionRequest(
            id=uuid4().hex[:12],
            request_type=run_type,
            task_id=definition_id,
            session_key=session_key,
            session_id=session_id,
            post_to=post_to,
            deliver_key=deliver_key,
            prompt=prompt,
            message=prompt,
            source_kind=source_kind,
            source_actor=source_actor,
            parent_run_id=parent_run_id,
            agent_name=agent_name,
            session_policy=session_policy,
            metadata=dict(metadata or {}),
        )
        if self._sqlite is None:
            with self._file_enqueue_lock:
                if (
                    suppress_scheduler_successor
                    and self._file_has_unstarted_scheduler_run(definition_id)
                ):
                    return None
                return self.enqueue(request)
        snapshot = self._sqlite.enqueue_definition_run(
            self.queued_run_payload(request),
            suppress_scheduler_successor=suppress_scheduler_successor,
            expected_run_at=expected_run_at,
            expected_timezone=expected_timezone,
            expected_updated_at=expected_updated_at,
            expected_job_id=expected_job_id,
            terminal_error=terminal_error,
        )
        if snapshot is None:
            return None
        for field_name in (
            "agent_name",
            "agent_id",
            "session_policy",
            "session_id",
            "session_key",
            "post_to",
            "deliver_key",
            "prompt",
            "message",
            "message_payload",
            "metadata",
        ):
            setattr(request, field_name, snapshot.get(field_name))
        return request

    def enqueue_hook_send(
        self,
        *,
        session_key: str,
        session_id: Optional[str] = None,
        prompt: str,
        post_to: Optional[str] = None,
        deliver_key: Optional[str] = None,
        agent_name: Optional[str] = None,
        session_policy: Optional[str] = None,
        run_type: str = "hook_send",
        definition_id: Optional[str] = None,
        source_kind: str = "cli",
        source_actor: Optional[str] = None,
        parent_run_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        expected_enabled_agent_id: Optional[str] = None,
        expected_reference_agent_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> TaskExecutionRequest:
        return self.enqueue(
            self.build_hook_send(
                session_key=session_key,
                session_id=session_id,
                prompt=prompt,
                post_to=post_to,
                deliver_key=deliver_key,
                agent_name=agent_name,
                agent_id=agent_id,
                session_policy=session_policy,
                run_type=run_type,
                definition_id=definition_id,
                source_kind=source_kind,
                source_actor=source_actor,
                parent_run_id=parent_run_id,
                metadata=metadata,
            ),
            expected_enabled_agent_id=expected_enabled_agent_id,
            expected_reference_agent_id=expected_reference_agent_id,
        )

    def build_hook_send(
        self,
        *,
        session_key: str,
        session_id: Optional[str] = None,
        prompt: str,
        post_to: Optional[str] = None,
        deliver_key: Optional[str] = None,
        agent_name: Optional[str] = None,
        agent_id: Optional[str] = None,
        session_policy: Optional[str] = None,
        run_type: str = "hook_send",
        definition_id: Optional[str] = None,
        source_kind: str = "cli",
        source_actor: Optional[str] = None,
        parent_run_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> TaskExecutionRequest:
        """Build a hook request WITHOUT queueing it.

        For the caller that must make the outbox row durable inside someone else's
        transaction (HFR-269): the request is composed here, so its shape cannot drift
        from ``enqueue_hook_send``, and becomes durable only where that caller commits.
        """

        return TaskExecutionRequest(
            id=uuid4().hex[:12],
            request_type=run_type,
            task_id=definition_id,
            session_key=session_key,
            session_id=session_id,
            post_to=post_to,
            deliver_key=deliver_key,
            prompt=prompt,
            message=prompt,
            source_kind=source_kind,
            source_actor=source_actor,
            parent_run_id=parent_run_id,
            agent_name=agent_name,
            agent_id=agent_id,
            session_policy=session_policy,
            metadata=dict(metadata or {}),
        )

    def enqueue_agent_run(
        self,
        *,
        message: str,
        agent_name: Optional[str] = None,
        agent_id: Optional[str] = None,
        agent_backend: Optional[str] = None,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        session_policy: Optional[str] = None,
        session_key: str = "",
        session_id: Optional[str] = None,
        post_to: Optional[str] = None,
        deliver_key: Optional[str] = None,
        source_kind: str = "cli",
        source_actor: Optional[str] = None,
        parent_run_id: Optional[str] = None,
        callback_session_id: Optional[str] = None,
        callback_active: bool = True,
        delivery_intent: str = AGENT_RUN_DELIVERY_STEER,
        metadata: Optional[dict[str, Any]] = None,
        expected_enabled_agent_id: Optional[str] = None,
        callback_parent_to_arm: Optional[str] = None,
    ) -> Optional[TaskExecutionRequest]:
        if not (message or "").strip():
            # Refuse at the door: a blank prompt never reaches an agent backend
            # (``MessageHandler`` returns early), so the run could never be settled
            # by a terminal result. Every caller (CLI, callback, delivery) has a
            # real message; a blank one is a bug in the caller.
            raise ValueError("agent run requires a non-empty message")
        normalized_delivery_intent = normalize_agent_run_delivery_intent(delivery_intent)
        run_metadata = dict(metadata or {})
        run_metadata[AGENT_RUN_DELIVERY_INTENT_METADATA_KEY] = normalized_delivery_intent
        request = TaskExecutionRequest(
            id=uuid4().hex[:12],
            request_type="agent_run",
            session_key=session_key,
            session_id=session_id,
            post_to=post_to,
            deliver_key=deliver_key,
            prompt=message,
            message=message,
            source_kind=source_kind,
            source_actor=source_actor,
            parent_run_id=parent_run_id,
            callback_session_id=callback_session_id,
            callback_status="pending" if callback_session_id and callback_active else None,
            agent_name=agent_name,
            agent_id=agent_id,
            agent_backend=agent_backend,
            model=model,
            reasoning_effort=reasoning_effort,
            session_policy=session_policy,
            metadata=run_metadata,
        )
        normalized_callback_parent = str(callback_parent_to_arm or "").strip()
        if normalized_callback_parent and self._sqlite is not None:
            callback_run_id = self._sqlite.enqueue_callback_run(
                self.queued_run_payload(request),
                parent_run_id=normalized_callback_parent,
                source_actor=str(source_actor or ""),
            )
            if callback_run_id is None:
                return None
            stored = self._sqlite.get_run(callback_run_id)
            return TaskExecutionRequest.from_dict(stored) if stored is not None else request
        if normalized_callback_parent:
            terminal_turn_id = str(
                run_metadata.get(CALLBACK_TERMINAL_TURN_ID_METADATA_KEY) or ""
            ).strip()
            existing = self.find_callback_run(
                parent_run_id=normalized_callback_parent,
                source_actor=str(source_actor or ""),
                terminal_turn_id=terminal_turn_id or None,
                callback_session_id=session_id,
            )
            if existing is not None:
                self.update_callback_status(
                    normalized_callback_parent,
                    status="sent",
                    callback_run_id=str(existing["id"]),
                )
                return TaskExecutionRequest.from_dict(existing)
            parent = self.get_run(normalized_callback_parent)
            if parent is None:
                raise ValueError(
                    f"callback parent Run not found: {normalized_callback_parent}"
                )
            parent_status = _normalize_requested_run_status(parent.get("status"))
            if bool(parent.get("cancel_requested")) and parent_status != "canceled":
                return None
        queued = self.enqueue(
            request,
            expected_enabled_agent_id=expected_enabled_agent_id,
        )
        if normalized_callback_parent:
            self.update_callback_status(
                normalized_callback_parent,
                status="sent",
                callback_run_id=queued.id,
            )
        return queued

    def list_pending(
        self,
        *,
        limit: int | None = None,
        after: tuple[str, str] | None = None,
    ) -> list[TaskExecutionRequest]:
        if self._sqlite is not None:
            return [
                TaskExecutionRequest.from_dict(item)
                for item in self._sqlite.list_runs(
                    status="pending",
                    run_types=tuple(EXECUTION_RUN_TYPES),
                    after=after,
                    limit=limit,
                )
                # ``task_escalation`` belongs here for the same reason ``hook_send``
                # does: it is a queued Agent turn the drain loop must claim. Left out,
                # the escalation row would be durable and never executed -- a failure
                # report the atomic stamp promised and nothing ever delivered.
                if item.get("request_type") in EXECUTION_RUN_TYPES
            ]
        self._ensure_dirs()
        requests: list[TaskExecutionRequest] = []
        for path in self.pending_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.error("Failed to read task request %s: %s", path, exc)
                continue
            if not isinstance(payload, dict):
                continue
            requests.append(TaskExecutionRequest.from_dict(payload))
        ordered = sorted(requests, key=lambda item: (item.created_at, item.id))
        if after is not None:
            ordered = [
                item
                for item in ordered
                if (item.created_at, item.id) > (str(after[0]), str(after[1]))
            ]
        return ordered if limit is None else ordered[: max(0, int(limit))]

    def _file_has_unstarted_scheduler_run(self, definition_id: str) -> bool:
        """Match SQLite's queued-or-claimed scheduler successor fence."""

        for run in self._list_file_runs():
            if (
                str(run.get("task_id") or run.get("definition_id") or "")
                != str(definition_id)
                or run.get("source_kind") != "scheduler"
            ):
                continue
            status = _normalize_requested_run_status(run.get("status"))
            if status == "queued" or (status == "running" and not run.get("pid")):
                return True
        return False

    def list_runs(self, *, status: Optional[str] = None) -> list[dict[str, Any]]:
        if self._sqlite is not None:
            return self._sqlite.list_runs(status=status)
        return self._list_file_runs(status=status)

    def get_unsettled_watch_run(self, definition_id: str) -> Optional[dict[str, Any]]:
        """Return the oldest queued/running Watch follow-up for the admission fence."""

        if self._sqlite is not None:
            return self._sqlite.get_unsettled_watch_run(definition_id)
        for run in self._list_file_runs():
            if str(run.get("task_id") or run.get("definition_id") or "") != str(
                definition_id
            ):
                continue
            if str(run.get("request_type") or run.get("run_type") or "") != "watch":
                continue
            status = _normalize_requested_run_status(run.get("status"))
            if status in {"queued", "running"}:
                return run
        return None

    def consecutive_definition_failures_with_code(
        self,
        definition_id: str,
        failure_code: str,
        *,
        limit: int,
    ) -> int:
        """Count the newest consecutive failed verdicts carrying one stable code."""

        if limit <= 0:
            return 0
        if self._sqlite is not None:
            return self._sqlite.consecutive_definition_failures_with_code(
                definition_id,
                failure_code,
                limit=limit,
            )
        verdicts = [
            run
            for run in self._list_file_runs()
            if str(run.get("definition_id") or run.get("task_id") or "") == str(definition_id)
            and (_normalize_requested_run_status(run.get("status")) or run.get("status")) in {"succeeded", "failed"}
            and not (
                isinstance(run.get("metadata"), dict)
                and run["metadata"].get("interrupt_reason") in RUN_INTERRUPTION_REASONS
            )
        ]
        count = 0
        for run in reversed(verdicts[-limit:]):
            if (_normalize_requested_run_status(run.get("status")) or run.get("status")) != "failed":
                break
            metadata = run.get("metadata")
            if not isinstance(metadata, dict) or metadata.get("failure_code") != failure_code:
                break
            count += 1
        return count

    def list_pending_callbacks(
        self,
        *,
        limit: int = 20,
        after: tuple[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        if self._sqlite is not None:
            return self._sqlite.list_pending_callbacks(limit=limit, after=after)
        runs = [
            item
            for item in self._list_file_runs()
            if item.get("callback_session_id")
            and item.get("callback_status") == "pending"
            and item.get("completed_at")
            and (_normalize_requested_run_status(item.get("status")) or item.get("status")) in TERMINAL_RUN_STATUSES
        ]
        ordered = sorted(
            runs,
            key=lambda item: (item.get("completed_at") or "", item.get("id") or ""),
        )
        if after is not None:
            ordered = [
                item
                for item in ordered
                if (item.get("completed_at") or "", item.get("id") or "")
                > (str(after[0]), str(after[1]))
            ]
        return ordered[:limit]

    def list_deferred_runs(self) -> list[dict[str, Any]]:
        if self._sqlite is not None:
            return self._sqlite.list_deferred_runs()
        return [
            run
            for run in self._list_file_runs()
            if isinstance(run.get("result_payload"), dict)
            and run["result_payload"].get("deferred_terminal_status")
            and (_normalize_requested_run_status(run.get("status")) or run.get("status"))
            not in TERMINAL_RUN_STATUSES
        ]

    def find_escalation_run(self, *, parent_run_id: str) -> Optional[dict[str, Any]]:
        """Return the escalation Run one command fire queued, whatever its status.

        The status is the CALLER's decision to make -- retracting one is only valid
        while it is still non-terminal -- so this reports the row rather than filtering
        it out and answering "there is none".
        """

        if self._sqlite is not None:
            return self._sqlite.find_escalation_run(parent_run_id=parent_run_id)
        for run in self._list_file_runs():
            if (
                run.get("request_type") == "task_escalation"
                and run.get("parent_run_id") == parent_run_id
            ):
                return run
        return None

    def find_callback_run(
        self,
        *,
        parent_run_id: str,
        source_actor: str,
        terminal_turn_id: Optional[str] = None,
        callback_session_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        if self._sqlite is not None:
            return self._sqlite.find_callback_run(
                parent_run_id=parent_run_id,
                source_actor=source_actor,
                terminal_turn_id=terminal_turn_id,
                callback_session_id=callback_session_id,
            )
        normalized_turn_id = str(terminal_turn_id or "").strip()
        normalized_session_id = str(callback_session_id or "").strip()
        for run in self._list_file_runs():
            metadata = run.get("metadata")
            if (
                normalized_turn_id
                and normalized_session_id
                and run.get("request_type") == "agent_run"
                and run.get("source_kind") == "callback"
                and run.get("session_id") == normalized_session_id
                and isinstance(metadata, dict)
                and metadata.get(CALLBACK_TERMINAL_TURN_ID_METADATA_KEY) == normalized_turn_id
            ):
                return run
            if (
                run.get("request_type") == "agent_run"
                and run.get("source_kind") == "callback"
                and run.get("parent_run_id") == parent_run_id
                and run.get("source_actor") == source_actor
            ):
                return run
        return None

    def settle_deferred_run(
        self,
        run_id: str,
        *,
        terminal_status: Optional[str] = None,
        error: Optional[str] = None,
    ) -> bool:
        if self._sqlite is None:
            return False
        return self._sqlite.settle_deferred_run(
            run_id,
            terminal_status=terminal_status,
            error=error,
        )

    def record_skip_reason(self, run_id: str, *, reason: str) -> bool:
        """Record why the drain deferred a queued run (SQLite only, no-op otherwise)."""

        if self._sqlite is None:
            return False
        return self._sqlite.record_run_skip_reason(run_id, reason=reason)

    def sweep_stale_runs(
        self,
        *,
        owned_run_ids: set[str],
        error_texts: dict[str, str],
        deliverable_run_ids: Optional[set[str]] = None,
        orphan_grace_seconds: int,
        queued_ttl_seconds: int,
    ) -> list[SweptRun]:
        """Terminalize provably stale runs. Empty for the legacy file store."""

        if self._sqlite is None:
            return []
        return self._sqlite.sweep_stale_runs(
            owned_run_ids=owned_run_ids,
            error_texts=error_texts,
            deliverable_run_ids=deliverable_run_ids,
            orphan_grace_seconds=orphan_grace_seconds,
            queued_ttl_seconds=queued_ttl_seconds,
        )

    def supports_guarded_settlement(self) -> bool:
        """Whether this store can terminalize a run without clobbering a cancel.

        Only the SQLite store has the status-scoped UPDATE behind
        :meth:`settle_without_result`. The legacy file store has no equivalent, so
        its callers must fall back to the ``complete()`` path instead.
        """

        return self._sqlite is not None

    def settle_without_result(
        self,
        run_id: str,
        *,
        terminal_status: str = "failed",
        error: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Optional[str]:
        """Terminalize a still-open run whose turn produced no terminal result.

        Returns the status actually written (``terminal_status``, or ``canceled``
        when a ``failed`` settlement met a pending cancel), or ``None`` when nothing
        was written because the row is missing, already terminal, or holds a
        deferred terminal status.
        """

        if self._sqlite is None:
            return None
        return self._sqlite.settle_run_terminal(
            run_id,
            terminal_status=terminal_status,
            error=error,
            metadata=metadata,
        )

    def defer_run_terminal(
        self,
        run_id: str,
        *,
        terminal_status: str,
        error: Optional[str] = None,
        result_text: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> bool:
        if self._sqlite is None:
            return False
        return self._sqlite.defer_run_terminal(
            run_id,
            terminal_status=terminal_status,
            error=error,
            result_text=result_text,
            metadata=metadata,
        )

    def update_callback_status(
        self,
        run_id: str,
        *,
        status: str,
        error: Optional[str] = None,
        callback_run_id: Optional[str] = None,
    ) -> None:
        if self._sqlite is not None:
            self._sqlite.update_callback_status(
                run_id,
                status=status,
                error=error,
                callback_run_id=callback_run_id,
            )
            return
        now = _utc_now_iso()
        for state in ("pending", "processing", "completed"):
            path = self._request_path(run_id, state=state)
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                payload = {"id": run_id}
            if not isinstance(payload, dict):
                payload = {"id": run_id}
            payload.update(
                {
                    "callback_status": status,
                    "callback_error": error,
                    "callback_run_id": callback_run_id if callback_run_id is not None else payload.get("callback_run_id"),
                    "callback_completed_at": now,
                    "updated_at": now,
                }
            )
            write_atomic(path, json.dumps(payload, indent=2))
            return

    def mark_callback_pending(self, run_id: str) -> None:
        if self._sqlite is not None:
            self._sqlite.mark_callback_pending(run_id)
            return
        now = _utc_now_iso()
        for state in ("pending", "processing", "completed"):
            path = self._request_path(run_id, state=state)
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                payload = {"id": run_id}
            if not isinstance(payload, dict):
                payload = {"id": run_id}
            payload.update(
                {
                    "callback_status": "pending",
                    "callback_error": None,
                    "callback_completed_at": None,
                    "updated_at": now,
                }
            )
            write_atomic(path, json.dumps(payload, indent=2))
            return

    def list_runs_page(
        self,
        *,
        status: Optional[str] = None,
        run_type: Optional[str] = None,
        agent_name: Optional[str] = None,
        agent_backend: Optional[str] = None,
        session_id: Optional[str] = None,
        definition_id: Optional[str] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
        query: Optional[str] = None,
        page_request: PageRequest | None,
        newest_first: bool = True,
    ) -> PageResult[dict[str, Any]]:
        if self._sqlite is not None:
            return self._sqlite.list_runs_page(
                status=status,
                run_type=run_type,
                agent_name=agent_name,
                agent_backend=agent_backend,
                session_id=session_id,
                definition_id=definition_id,
                created_after=created_after,
                created_before=created_before,
                query=query,
                page_request=page_request,
                newest_first=newest_first,
            )
        runs = self._list_file_runs(status=status)
        if run_type:
            runs = [item for item in runs if (item.get("run_type") or item.get("request_type")) == run_type]
        if agent_name:
            runs = [item for item in runs if item.get("agent_name") == agent_name]
        if agent_backend:
            runs = [item for item in runs if item.get("agent_backend") == agent_backend]
        if session_id:
            runs = [item for item in runs if item.get("session_id") == session_id]
        if definition_id:
            runs = [item for item in runs if (item.get("definition_id") or item.get("task_id")) == definition_id]
        if created_after:
            runs = [item for item in runs if str(item.get("created_at") or "") >= created_after]
        if created_before:
            runs = [item for item in runs if str(item.get("created_at") or "") <= created_before]
        if query:
            needle = query.casefold()
            fields = ("id", "definition_id", "task_id", "agent_name", "session_id", "prompt", "message", "result_text", "error", "stdout", "stderr")
            runs = [
                item
                for item in runs
                if any(needle in str(item.get(field) or "").casefold() for field in fields)
            ]
        runs = sorted(runs, key=lambda item: (item.get("created_at") or "", item.get("id") or ""), reverse=newest_first)
        return page_sequence(runs, page_request)

    def _list_file_runs(self, *, status: Optional[str] = None) -> list[dict[str, Any]]:
        status_filter = _run_file_state_for_status(status)
        runs: list[dict[str, Any]] = []
        for state, directory in {
            "pending": self.pending_dir,
            "processing": self.processing_dir,
            "completed": self.completed_dir,
        }.items():
            if status_filter and status_filter != state:
                continue
            if not directory.exists():
                continue
            for path in directory.glob("*.json"):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if isinstance(payload, dict):
                    normalized_status = _normalize_file_run_status(payload, state)
                    requested_status = _normalize_requested_run_status(status)
                    if requested_status and normalized_status != requested_status:
                        continue
                    payload["status"] = normalized_status
                    runs.append(payload)
        return sorted(runs, key=lambda item: (item.get("created_at") or "", item.get("id") or ""))

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        if self._sqlite is not None:
            return self._sqlite.get_run(run_id)
        for item in self.list_runs():
            if item.get("id") == run_id:
                return item
        return None

    def record_run_activity(self, run_ids: Sequence[str]) -> list[str]:
        """Persist activity on the shared SQLite ledger when available."""

        if self._sqlite is None:
            return []
        return self._sqlite.record_run_activity(run_ids)

    def cancel_run(self, run_id: str) -> bool:
        if self._sqlite is not None:
            return self._sqlite.cancel_run(run_id)
        now = _utc_now_iso()
        pending_path = self._request_path(run_id, state="pending")
        if pending_path.exists():
            try:
                payload = json.loads(pending_path.read_text(encoding="utf-8"))
            except Exception:
                payload = {"id": run_id}
            if not isinstance(payload, dict):
                payload = {"id": run_id}
            payload.update(
                {
                    "id": run_id,
                    "status": "canceled",
                    "cancel_requested": True,
                    "cancel_requested_at": now,
                    "completed_at": now,
                    "updated_at": now,
                }
            )
            completed_path = self._request_path(run_id, state="completed")
            write_atomic(completed_path, json.dumps(payload, indent=2))
            pending_path.unlink(missing_ok=True)
            return True

        processing_path = self._request_path(run_id, state="processing")
        if processing_path.exists():
            try:
                payload = json.loads(processing_path.read_text(encoding="utf-8"))
            except Exception:
                payload = {"id": run_id}
            if not isinstance(payload, dict):
                payload = {"id": run_id}
            payload.update(
                {
                    "id": run_id,
                    "cancel_requested": True,
                    "cancel_requested_at": now,
                    "updated_at": now,
                }
            )
            write_atomic(processing_path, json.dumps(payload, indent=2))
            return True
        return False

    def mark_run_canceled(self, run_id: str, *, completed_at: Optional[str] = None) -> bool:
        now = completed_at or _utc_now_iso()
        existing = self.get_run(run_id)
        if existing is None:
            return False
        cancel_requested_at = str(existing.get("cancel_requested_at") or now)
        if self._sqlite is not None:
            self._sqlite.update_run_status(
                run_id,
                status="canceled",
                completed_at=now,
                updated_at=now,
                cancel_requested=True,
                cancel_requested_at=cancel_requested_at,
            )
            return True

        for state in ("pending", "processing", "completed"):
            source_path = self._request_path(run_id, state=state)
            if not source_path.exists():
                continue
            try:
                payload = json.loads(source_path.read_text(encoding="utf-8"))
            except Exception:
                payload = {"id": run_id}
            if not isinstance(payload, dict):
                payload = {"id": run_id}
            payload.update(
                {
                    "id": run_id,
                    "status": "canceled",
                    "cancel_requested": True,
                    "cancel_requested_at": payload.get("cancel_requested_at") or now,
                    "completed_at": now,
                    "updated_at": now,
                }
            )
            completed_path = self._request_path(run_id, state="completed")
            write_atomic(completed_path, json.dumps(payload, indent=2))
            if state != "completed":
                source_path.unlink(missing_ok=True)
            return True
        return False

    def claim(self, request_id: str) -> Optional[TaskExecutionRequest]:
        if self._sqlite is not None:
            now = _utc_now_iso()
            payload = self._sqlite.claim_pending_run(request_id, started_at=now)
            if payload is None:
                return None
            return TaskExecutionRequest.from_dict(payload)
        pending_path = self._request_path(request_id, state="pending")
        processing_path = self._request_path(request_id, state="processing")
        if not pending_path.exists():
            return None
        pending_path.replace(processing_path)
        payload = json.loads(processing_path.read_text(encoding="utf-8"))
        return TaskExecutionRequest.from_dict(payload)

    def refresh_claimed_request(self, request: TaskExecutionRequest) -> TaskExecutionRequest:
        """Refresh the Agent name that a catalog rename may rewrite after claim."""

        if self._sqlite is None:
            return request
        payload = self._sqlite.refresh_run_agent_reference(request.id)
        if payload is None:
            return request
        request.agent_name = payload.get("agent_name")
        request.agent_id = payload.get("agent_id")
        return request

    def requeue(self, request_id: str, *, metadata: Optional[dict[str, Any]] = None) -> None:
        if self._sqlite is not None:
            self._sqlite.mark_run_queued_from_running(
                request_id,
                updated_at=_utc_now_iso(),
                metadata=metadata,
            )
            return
        processing_path = self._request_path(request_id, state="processing")
        pending_path = self._request_path(request_id, state="pending")
        if not processing_path.exists():
            return
        if pending_path.exists():
            processing_path.unlink(missing_ok=True)
            return
        processing_path.replace(pending_path)

    def complete(
        self,
        request: TaskExecutionRequest,
        *,
        ok: bool,
        error: Optional[str] = None,
        task_id: Optional[str] = None,
        session_key: Optional[str] = None,
        session_id: Optional[str] = None,
        interrupt_reason: Optional[str] = None,
        failure_code: Optional[str] = None,
        exit_code: Optional[int] = None,
        stdout: Optional[str] = None,
        stderr: Optional[str] = None,
        timed_out: Optional[bool] = None,
        escalation_run_id: Optional[str] = None,
    ) -> Optional[str]:
        """Settle one claimed request.

        ``interrupt_reason`` is the caller's structured CLASS for a failure that has
        one, such as ``delivery_target_missing`` or an infrastructure teardown cause.
        It rides the SAME statement as the terminal transition, because that statement
        is also the one that stamps the owed failure notice: ``_merge_owed_failure_notice``
        applies ``extra_metadata`` to the run's ``metadata_json`` BEFORE
        ``_owed_failure_notice_for_transition`` reads ``interrupt_reason`` out of it, so
        the notice is born already carrying its class. A second UPDATE afterwards would
        be too late — the notice never overwrites an existing one, by design, so a class
        written after the stamp would be recorded on the run and missing from the very
        blob the drain renders from.

        Named keywords rather than a general ``metadata`` dict, deliberately: these
        arguments are merged verbatim into a run's metadata, and a wide-open
        passthrough at a completion site is how an unrelated key comes to overwrite
        ``interrupt_reason``, ``failure_code`` or the notice blob itself.

        ``exit_code`` / ``stdout`` / ``stderr`` are a command fire's outcome facts and
        stay ``None`` for every other request type.

        ``escalation_run_id`` names the already-durable Agent turn that carries this
        failure's report (``--on-failure agent``). It rides the metadata channel for
        exactly the reason ``interrupt_reason`` does: ``_merge_owed_failure_notice``
        applies ``extra_metadata`` BEFORE ``_owed_failure_notice_for_transition`` reads
        it, so the marker is in place when the notice decision is made and the same
        failure is not reported twice -- once as a turn, once as an alert.

        Returns the exact terminal status this call wrote, or ``None`` when another
        terminal owner already won.
        """

        extra_metadata: dict[str, Any] = {"ok": ok}
        if interrupt_reason:
            extra_metadata["interrupt_reason"] = interrupt_reason
        if failure_code:
            extra_metadata["failure_code"] = failure_code
        if timed_out is not None:
            extra_metadata[COMMAND_TIMED_OUT_METADATA_KEY] = bool(timed_out)
        if escalation_run_id:
            extra_metadata["escalation_run_id"] = escalation_run_id
        if self._sqlite is not None:
            # Guarded, NOT ``update_run_status``: that writer's UPDATE has no status
            # predicate, so an ordinary completion rewrote a status another actor had
            # already settled — a real terminal ``succeeded`` result became ``failed``
            # whenever the claimed-request layer finished with an error. The identity
            # columns are still written either way (see ``settle_run_terminal``), so
            # routing through the guard costs nothing a caller depended on.
            return self._sqlite.settle_run_terminal(
                request.id,
                terminal_status="succeeded" if ok else "failed",
                error=error,
                updated_at=_utc_now_iso(),
                task_id=task_id if task_id is not None else request.task_id,
                session_key=session_key if session_key is not None else request.session_key,
                session_id=session_id if session_id is not None else request.session_id,
                metadata=extra_metadata,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
            )
        processing_path = self._request_path(request.id, state="processing")
        completed_path = self._request_path(request.id, state="completed")
        if not processing_path.exists():
            return None
        terminal_status = "succeeded" if ok else "failed"
        payload = request.to_dict()
        self._carry_inflight_writes(processing_path, payload)
        payload.update(
            {
                "ok": ok,
                "error": error,
                "completed_at": _utc_now_iso(),
                "task_id": task_id if task_id is not None else request.task_id,
                "session_key": session_key if session_key is not None else request.session_key,
                "session_id": session_id if session_id is not None else request.session_id,
                "callback_session_id": request.callback_session_id,
            }
        )
        # Command outcome facts only when this fire produced them, so a message task's
        # completed JSON keeps exactly the keys it has always had.
        if exit_code is not None:
            payload["exit_code"] = exit_code
        if stdout is not None:
            payload["stdout"] = stdout
        if stderr is not None:
            payload["stderr"] = stderr
        if interrupt_reason or failure_code or timed_out is not None or escalation_run_id:
            # The file backend has no owed-notice machinery at all, so this records the
            # class where its only reader — an operator looking at the completed JSON —
            # can see it, rather than dropping the one fact the caller went to the
            # trouble of classifying. ``escalation_run_id`` rides the same channel: it
            # suppresses nothing here (there is nothing to suppress), but the pointer
            # from a failure to the turn that reports it is the same kind of fact.
            existing = payload.get("metadata")
            payload["metadata"] = {
                **(existing if isinstance(existing, dict) else {}),
                **({"interrupt_reason": interrupt_reason} if interrupt_reason else {}),
                **({"failure_code": failure_code} if failure_code else {}),
                **(
                    {COMMAND_TIMED_OUT_METADATA_KEY: bool(timed_out)}
                    if timed_out is not None
                    else {}
                ),
                **({"escalation_run_id": escalation_run_id} if escalation_run_id else {}),
            }
        write_atomic(completed_path, json.dumps(payload, indent=2))
        processing_path.unlink(missing_ok=True)
        return terminal_status

    @staticmethod
    def _carry_inflight_writes(processing_path: Path, payload: dict[str, Any]) -> None:
        """Layer what was written to this run WHILE it ran onto its terminal payload.

        ``complete`` composes that payload from the caller's ``TaskExecutionRequest``,
        which is the copy taken at ENQUEUE: every in-flight write went to the processing
        file and to nothing the caller holds. ``record_command_snapshot`` is one --
        SCT-028's re-stamp of the command the executor really spawned, after its
        post-claim reload of the definition -- so starting from the request alone
        discarded it, and the completed run reported the definition as it stood BEFORE
        an edit committed in that window. SQLite has no such gap: the metadata lives on
        the row ``settle_run_terminal`` updates in place and is carried forward for free,
        which is exactly why this backend has to do it deliberately. Same reason the
        recovery writer (``_settle_processing_file``) composes from the file, and the two
        terminal writers must not disagree about what a settled row remembers.

        The file wins on ``metadata`` key by key rather than wholesale, so a key only
        the request carries is not dropped by a store whose in-flight merges never saw
        it, and the caller's own outcome facts still land afterwards. ``pid`` and
        ``started_at`` come along for the same reason the metadata does: they exist only
        because ``mark_execution_started`` wrote them here.
        """

        try:
            stored = json.loads(processing_path.read_text(encoding="utf-8"))
        except Exception:
            # The payload composed from the request is still a truthful terminal record
            # of the fire; it just forgets what ran. Failing the completion instead would
            # leave the row ``running`` over bookkeeping.
            logger.debug(
                "failed to read in-flight state for %s while completing it",
                processing_path.name,
                exc_info=True,
            )
            return
        if not isinstance(stored, dict):
            return
        stored_metadata = stored.get("metadata")
        if isinstance(stored_metadata, dict):
            existing = payload.get("metadata")
            payload["metadata"] = {
                **(existing if isinstance(existing, dict) else {}),
                **stored_metadata,
            }
        for key in ("pid", "started_at"):
            if stored.get(key) is not None:
                payload[key] = stored[key]

    def settle_turn_participants(
        self,
        request: TaskExecutionRequest,
        run_ids: list[str],
        *,
        ok: bool,
        error: Optional[str] = None,
    ) -> None:
        if self._sqlite is not None:
            from storage.background import (
                settle_agent_runs_for_turn_in_connection,
                run_update_event_transaction,
            )

            with run_update_event_transaction(self._sqlite.engine) as conn:
                settle_agent_runs_for_turn_in_connection(
                    conn,
                    run_ids,
                    ok=ok,
                    error=error,
                )
            return
        self.complete(request, ok=ok, error=error)


class _ScheduledRuntimeWorkHandler(RuntimeWorkHandler):
    """Thin lane adapter around the existing durable owners.

    Scans run in the supervisor's executor. Processing remains on the controller
    loop only where it must create/cancel asyncio tasks or perform transport I/O.
    """

    def __init__(self, service: "ScheduledTaskService", lane: RuntimeWorkLane) -> None:
        self.service = service
        self.lane = lane
        self._fallback = (
            FallbackRequestRecoveryHandler(
                service.request_store.sqlite_backend,
                live_claims=lambda: service._live_claimed_run_ids,
                partition_key=service._fallback_request_partition_key,
            )
            if lane is RuntimeWorkLane.REQUESTS
            and service.request_store.sqlite_backend is not None
            else None
        )

    def scan(
        self,
        *,
        limit: int,
        occupied: frozenset[str],
        cursor: str | None,
    ) -> tuple[list[RuntimeWorkItem], bool]:
        if self.lane is RuntimeWorkLane.TASK_DEFINITIONS:
            changed = self.service.store.maybe_reload()
            return [
                RuntimeWorkItem(
                    "scheduled-tasks",
                    changed,
                    rearm_after_process=False,
                )
            ], False
        if self.lane is RuntimeWorkLane.REQUESTS:
            return self._scan_requests(limit=limit, occupied=occupied, cursor=cursor)
        if self.lane is RuntimeWorkLane.RUN_CALLBACKS:
            return self._scan_run_callbacks(limit=limit, occupied=occupied, cursor=cursor)
        if self.lane is RuntimeWorkLane.VAULT_CALLBACKS:
            return self.service._scan_vault_callback_work(
                limit=limit,
                occupied=occupied,
                cursor=cursor,
            )
        if self.lane is RuntimeWorkLane.ACTIVITY_OUTPUTS:
            registry = self.service._activity_registry()
            if registry is None:
                return [], False
            runtimes, has_more, retry_after, scanned_cursor = (
                registry.scan_recovered_output_runtimes(
                    limit=limit,
                    cursor=cursor,
                    grace_seconds=self.service._activity_output_grace_seconds,
                )
            )
            if retry_after is not None:
                self.service._schedule_runtime_work_wake(
                    RuntimeWorkLane.ACTIVITY_OUTPUTS,
                    retry_after,
                )
            items = [
                RuntimeWorkItem(
                    f"{backend}\x1f{runtime_key}",
                    (backend, runtime_key),
                    cursor_key=f"{backend}\x1f{runtime_key}",
                )
                for backend, runtime_key in runtimes
            ]
            if scanned_cursor and (
                not items or items[-1].cursor_key != scanned_cursor
            ):
                items.append(
                    RuntimeWorkItem(
                        "",
                        None,
                        cursor_key=scanned_cursor,
                        rearm_after_process=False,
                        cursor_only=True,
                    )
                )
            return items, has_more
        if self.lane is RuntimeWorkLane.FAILURE_NOTICES:
            return self._scan_failure_notices(
                limit=limit,
                occupied=occupied,
                cursor=cursor,
            )
        if self.lane is RuntimeWorkLane.STALE_RUNS:
            retry_after = self.service._stale_run_sweep_delay_seconds()
            if retry_after is None:
                return [
                    RuntimeWorkItem(
                        "run-cancellations",
                        None,
                        rearm_after_process=False,
                    )
                ], False
            if retry_after > 0:
                self.service._schedule_runtime_work_wake(
                    RuntimeWorkLane.STALE_RUNS,
                    retry_after,
                )
                return [
                    RuntimeWorkItem(
                        "run-cancellations",
                        None,
                        rearm_after_process=False,
                    )
                ], False
            return [
                RuntimeWorkItem(
                    "stale-runs",
                    None,
                    rearm_after_process=False,
                )
            ], False
        return [], False

    def _scan_requests(
        self,
        *,
        limit: int,
        occupied: frozenset[str],
        cursor: str | None,
    ) -> tuple[list[RuntimeWorkItem], bool]:
        items: list[RuntimeWorkItem] = []
        fallback_has_more = False
        if self._fallback is not None:
            fallback_cursor = None
            scan_fallback = cursor is None or cursor.startswith("0\x1f")
            if cursor and scan_fallback:
                fallback_cursor = cursor.split("\x1f", 1)[1]
            recovered, fallback_has_more = (
                self._fallback.scan(
                    limit=limit,
                    occupied=occupied,
                    cursor=fallback_cursor,
                )
                if scan_fallback
                else ([], False)
            )
            items.extend(
                [
                    RuntimeWorkItem(
                        item.partition_key,
                        ("fallback", item.observation),
                        cursor_key=f"0\x1f{item.cursor_key or item.partition_key}",
                    )
                    for item in recovered
                ]
            )

        remaining = max(0, limit - len(items))
        if remaining == 0:
            return items, fallback_has_more
        fallback_continuation = (
            items[-1].cursor_key
            if fallback_has_more and items
            else None
        )

        after = None
        if cursor and cursor.startswith("1\x1f"):
            parts = cursor.split("\x1f", 2)
            if len(parts) == 3:
                after = (parts[1], parts[2])
        pending = self.service.request_store.list_pending(limit=remaining, after=after)
        items.extend(
            [
                RuntimeWorkItem(
                    self.service._request_partition_key(request),
                    ("queued", request),
                    cursor_key=(
                        fallback_continuation
                        or f"1\x1f{request.created_at}\x1f{request.id}"
                    ),
                )
                for request in pending
            ]
        )
        return items, fallback_has_more or len(pending) >= remaining

    def _scan_run_callbacks(
        self,
        *,
        limit: int,
        occupied: frozenset[str],
        cursor: str | None,
    ) -> tuple[list[RuntimeWorkItem], bool]:
        after = None
        if cursor:
            parts = cursor.split("\x1f", 1)
            if len(parts) == 2:
                after = (parts[0], parts[1])
        rows = self.service.request_store.list_pending_callbacks(
            limit=limit,
            after=after,
        )
        items: list[RuntimeWorkItem] = []
        for row in rows:
            run_id = str(row.get("id") or "")
            session_id = str(row.get("callback_session_id") or "")
            partition = f"session:{session_id}" if session_id else f"run:{run_id}"
            if run_id:
                items.append(
                    RuntimeWorkItem(
                        partition,
                        row,
                        cursor_key=(
                            f"{row.get('completed_at') or ''}\x1f{run_id}"
                        ),
                    )
                )
        return items, len(rows) >= limit

    @staticmethod
    def _failure_notice_cursor(row: dict[str, Any]) -> tuple[str, str, str]:
        metadata = row.get("metadata")
        notice = (
            metadata.get(OWED_FAILURE_NOTICE_KEY)
            if isinstance(metadata, dict)
            else None
        )
        return (
            str(notice.get("next_attempt_at") or "")
            if isinstance(notice, dict)
            else "",
            str(row.get("created_at") or ""),
            str(row.get("id") or ""),
        )

    def _scan_failure_notices(
        self,
        *,
        limit: int,
        occupied: frozenset[str],
        cursor: str | None,
    ) -> tuple[list[RuntimeWorkItem], bool]:
        store = self.service.request_store.sqlite_backend
        if store is None:
            return [], False
        after = None
        if cursor:
            parts = cursor.split("\x1f", 2)
            if len(parts) == 3:
                after = (parts[0], parts[1], parts[2])
        rows = store.list_owed_failure_notices(limit=limit, after=after)
        next_attempt_at = store.next_owed_failure_notice_at()
        if next_attempt_at:
            try:
                next_attempt = datetime.fromisoformat(next_attempt_at)
                if next_attempt.tzinfo is None:
                    next_attempt = next_attempt.replace(tzinfo=timezone.utc)
                delay = max(
                    0.0,
                    (next_attempt.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds(),
                )
                self.service._schedule_runtime_work_wake(
                    RuntimeWorkLane.FAILURE_NOTICES,
                    delay,
                )
            except (TypeError, ValueError):
                logger.warning(
                    "Ignoring invalid failure-notice retry timestamp %r",
                    next_attempt_at,
                )
        items: list[RuntimeWorkItem] = []
        for row in rows:
            run_id = str(row.get("id") or "")
            metadata = row.get("metadata")
            notice = (
                metadata.get(OWED_FAILURE_NOTICE_KEY)
                if isinstance(metadata, dict)
                else None
            )
            definition_id = str(
                row.get("definition_id") or row.get("task_id") or ""
            )
            turn_id = failure_notices.notice_turn_id(notice)
            if turn_id:
                partition = f"turn:{turn_id}"
            elif definition_id and not failure_notices.bypasses_suppression(notice):
                partition = f"definition:{definition_id}"
            else:
                partition = f"run:{run_id}"
            if run_id:
                cursor_parts = self._failure_notice_cursor(row)
                items.append(
                    RuntimeWorkItem(
                        partition,
                        row,
                        cursor_key="\x1f".join(cursor_parts),
                    )
                )
        return items, len(rows) >= limit

    async def process(self, item: RuntimeWorkItem) -> bool | None:
        if self.lane is RuntimeWorkLane.TASK_DEFINITIONS:
            self.service.reconcile_jobs()
            return True
        if self.lane is RuntimeWorkLane.REQUESTS:
            kind, observation = item.observation
            if kind == "fallback":
                assert self._fallback is not None
                return await self.service._run_runtime_sync(
                    self._fallback.process_sync,
                    RuntimeWorkItem(item.partition_key, observation),
                )
            return await self.service._process_pending_request(observation)
        if self.lane is RuntimeWorkLane.RUN_CALLBACKS:
            return await self.service._process_run_callback(item.observation)
        if self.lane is RuntimeWorkLane.VAULT_CALLBACKS:
            return await self.service._process_vault_callback(item.observation)
        if self.lane is RuntimeWorkLane.ACTIVITY_OUTPUTS:
            backend, runtime_key = item.observation
            return await self.service._process_recovered_activity_output(
                backend,
                runtime_key,
            )
        if self.lane is RuntimeWorkLane.FAILURE_NOTICES:
            store = self.service.request_store.sqlite_backend
            if store is not None:
                await self.service._deliver_one_failure_notice(store, item.observation)
            return True
        if self.lane is RuntimeWorkLane.STALE_RUNS:
            if item.partition_key == "run-cancellations":
                await self.service._propagate_requested_cancellations_async()
                return True
            await self.service._propagate_requested_cancellations_async()
            await self.service._run_runtime_sync(
                self.service._sweep_stale_runs,
                release_leaked_locks=False,
            )
            self.service._release_leaked_session_locks()
            retry_after = self.service._stale_run_sweep_delay_seconds()
            if retry_after is not None and retry_after > 0:
                self.service._schedule_runtime_work_wake(
                    RuntimeWorkLane.STALE_RUNS,
                    retry_after,
                )
            return True
        return True


class ScheduledTaskService:
    """Controller-owned runtime that executes persisted scheduled tasks."""

    # Upper bound on claimed requests executing concurrently. The drain loop
    # never blocks waiting on an execution: when this many are in flight it
    # simply leaves the rest queued and re-checks on the next tick. This caps
    # fan-out without re-introducing head-of-line blocking.
    _MAX_CONCURRENT_EXECUTIONS = 8
    #: Starts allowed to try killing one surviving command worker. Retrying is what
    #: gives an unkillable orphan a second chance at all; the cap is what stops its
    #: run from being held ``running`` for the life of the install.
    _MAX_COMMAND_WORKER_REAP_ATTEMPTS = 3

    def __init__(
        self,
        controller,
        store: Optional[ScheduledTaskStore] = None,
        request_store: Optional[TaskExecutionStore] = None,
    ):
        self.controller = controller
        self.store = store or ScheduledTaskStore()
        self.request_store = request_store or TaskExecutionStore()
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self.scheduler.add_listener(
            self._on_scheduler_event, EVENT_JOB_MISSED | EVENT_JOB_ERROR
        )
        self._reconcile_task: Optional[asyncio.Task] = None
        self._service_teardown_task: Optional["asyncio.Task[None]"] = None
        self._request_recovery_token: RuntimeWorkRegistrationToken | None = None
        self._request_recovery_unregistration_task: asyncio.Task[None] | None = None
        self._runtime_work_tokens: dict[
            RuntimeWorkLane, RuntimeWorkRegistrationToken
        ] = {}
        self._controller_runtime_work_tokens: dict[
            RuntimeWorkLane, RuntimeWorkRegistrationToken
        ] = {}
        self._legacy_probe_tasks: dict[str, asyncio.Task[None]] = {}
        self._legacy_task_signature: tuple[int, int, int] | None = None
        self._legacy_request_signature: tuple[_DirectoryEntrySignature, ...] | None = None
        # The owed-notice drain pass currently in flight, if any. It runs OUTSIDE the
        # store watch (see ``_spawn_failure_notice_drain``), which means it also has to
        # be torn down by name: cancelling the watch no longer stops it.
        self._notice_drain_task: Optional["asyncio.Task[Any]"] = None
        self._job_signatures: Dict[str, tuple[Any, ...]] = {}
        # Logical Task id -> the physical APScheduler job generation. One-shot
        # job ids are unique per registration so a queued event from a replaced
        # DateTrigger cannot be mistaken for its successor.
        self._job_ids: Dict[str, str] = {}
        self._one_shot_job_identities: Dict[str, tuple[str, str, str, str]] = {}
        self._running = False
        self._watch_store_restart_count = 0
        # Claimed requests currently executing, keyed by request id, so a
        # single slow/hung turn can't stall delivery of every other request.
        self._inflight_executions: Dict[str, "asyncio.Task[Any]"] = {}
        self._request_capacity_reservations: set[str] = set()
        # Immutable loop-published view consumed by the off-thread recovery scan.
        # The handler rechecks it on the loop before the recovery CAS.
        self._live_claimed_run_ids: frozenset[str] = frozenset()
        # The exact claims behind those tasks. The done callback uses this as a
        # terminalization backstop when asyncio cancels a task before its coroutine
        # executes even one line.
        self._inflight_requests: Dict[str, TaskExecutionRequest] = {}
        # request id -> infrastructure cause chosen before cancellation. A raw
        # cancellation falls back to ``interrupted`` in the execution wrapper.
        self._inflight_cancellation_causes: Dict[str, str] = {}
        # Canonical conversation keys with an execution in flight. Used to
        # serialize turns per session (never two at once for the same
        # conversation) while still running different sessions concurrently.
        self._inflight_sessions: set[str] = set()
        # lock key -> the request id that took it. The set above answers "is this
        # conversation busy"; this answers "busy on behalf of WHOM", which is what
        # lets the sweep release a leaked lock without ever freeing one a live
        # execution still holds (see ``_release_leaked_session_locks``).
        self._session_lock_owners: Dict[str, str] = {}
        # Cache of session_id -> canonical lock key (resolution hits SQLite).
        self._session_lock_cache: Dict[str, str] = {}
        self._pending_recovered_activity_terminals: list[Any] = []
        # Monotonic timestamp of the last staleness sweep, so the sweep can ride the
        # 2 s store tick while running at most once per configured interval.
        self._last_sweep_at: float = 0.0
        # Warn once, not once per interval, while the sweep is failing closed.
        self._sweep_ownership_unavailable_logged = False
        self._requires_service_lease = runtime.service_instance_lock_attached_to_process()
        self._drain_dirty = True
        self._recover_activity_lifecycle()

    def recover_processing_requests(self) -> None:
        """Reconcile fallback Run owners after backend and Turn recovery."""

        # BEFORE ``recover_processing``, which nulls ``pid`` and settles these rows:
        # the run is the only place the orphan's identity is recorded, so once it is
        # terminal the child can never be found again. This pass decides each record by
        # WRITING it -- cleared once the process is proven dead, re-stamped while it may
        # still be running -- and ``recover_processing`` then leaves exactly the rows
        # whose record survived, so the NEXT start can try again.
        self._reap_orphaned_command_workers()
        self.request_store.recover_processing(
            interruption_error=self._t(SETTLEMENT_I18N_KEYS[SETTLED_BY_RESTARTED]),
            cancellation_error=self._t(SETTLEMENT_I18N_KEYS[SETTLED_BY_STOPPED]),
        )

    def _t(self, key: str, **kwargs: Any) -> str:
        """Translate a user-visible string in the configured language.

        Mirrors ``UpdateChecker._t``: read the language straight off the
        controller's config so a headless service (tests, CLI-only runs) with no
        controller still falls back to English instead of raising.
        """

        config = getattr(self.controller, "config", None)
        lang = str(getattr(config, "language", "en") or "en")
        return i18n_t(key, lang, **kwargs)

    def _localize_worker_error(self, stderr: Optional[str]) -> str:
        """The shared worker-error decoder, reading this lane's copy.

        Both lanes spawn the SAME supervisor, so both can receive its
        machine-readable error line -- see ``core.watch_worker.localize_worker_error``.
        The ``harness.command`` namespace is what keeps the sentence honest: a user
        managing a cron job has no watch worker, and should not be told about one.
        Ordinary command stderr passes through untouched.
        """

        return localize_worker_error(
            stderr or "", self._t, namespace="harness.command"
        )

    def _require_command_worker_record(
        self,
        execution_id: str,
        identity: Optional[PersistedProcessIdentity],
    ) -> None:
        """Refuse to run a command whose worker could not be durably named.

        THE ONE MOMENT THIS CAN BE REFUSED FOR FREE. The supervisor is blocked reading
        its spec off stdin, so the backup, deployment or migration has not started: an
        exception here costs the user a reported failure, while continuing costs the
        ability to ever find the process again if this service dies mid-command -- the
        unrecoverable side of the same trade SCT-029/031/032 keep landing on. The runner
        reaps the tree on the way out (the callback runs inside its protected block).

        An UNCAPTURED identity is not that case and does not refuse. Its dominant cause
        is a supervisor that has already exited -- ``psutil.NoSuchProcess`` -- where there
        is no process to name and the handshake is about to report the worker's own,
        better error; and an identity that could not be captured cannot be made
        trustworthy by persisting it, because every kill path refuses a record it cannot
        fully vouch for (``process_identity_from_payload``).

        A refused stamp (``False``) means the row is no longer ``running``: the fire was
        stopped or already settled, so nothing would ever read the record anyway. Failing
        is right there too, and costs the user nothing -- a stop normalizes this fire to
        ``canceled``, which owes no notice (SCT-027).
        """

        if identity is None:
            logger.warning(
                "command fire %s has no durable worker identity; a crash during this "
                "command would leave it unfindable",
                execution_id,
            )
            return
        try:
            stamped = self.request_store.record_command_worker(
                execution_id, serialize_process_identity(identity)
            )
        except Exception as exc:
            raise RuntimeError(self._t("harness.command.workerNotRecorded")) from exc
        if not stamped:
            raise RuntimeError(self._t("harness.command.workerNotRecorded"))

    def _clear_command_worker(self, execution_id: str) -> None:
        """Drop a fire's worker record, never failing the fire.

        Best-effort on purpose, and the opposite trade from the stamp: this runs when
        the process is gone or proven dead, so the record it removes protects nothing,
        and a store hiccup must not turn a command that ran fine into a reported
        failure. A record left behind by such a hiccup is harmless -- the next start
        finds a pid it cannot vouch for and drops it.
        """

        try:
            self.request_store.record_command_worker(execution_id, None)
        except Exception:
            logger.debug(
                "failed to clear command worker for %s", execution_id, exc_info=True
            )

    def _record_executed_command(self, execution_id: str, task: ScheduledTask) -> None:
        """Correct the run's command snapshot to the definition actually being run.

        The enqueue stamped the definition as it stood THEN; this stamps it as it
        stands at the moment of execution, which is the copy the fire will really
        spawn (``_execute_claimed_request`` re-reads the definition after claiming).
        An edit committed in that window otherwise left the notice, the escalation
        prompt and the Workbench run detail naming a command that never ran.

        Best-effort like the worker stamp: a store hiccup must degrade the record,
        not fail a command that is about to run correctly.
        """

        try:
            self.request_store.record_command_snapshot(
                execution_id, command_snapshot(task)
            )
        except Exception:
            logger.debug(
                "failed to record executed command for %s", execution_id, exc_info=True
            )

    def _reap_orphaned_command_workers(self) -> None:
        """Kill command workers this host left running when the service died.

        Identity, not pid: between the crash and this pass the OS is free to reuse
        the number, so a bare ``kill(pid)`` here would be a coin flip on the user's
        other processes. Every kill path inside ``reap_orphaned_process_tree``
        re-reads the live start time and ``AVIBE_PROCESS_IDENTITY`` marker and
        refuses on any mismatch, and ``process_identity_from_payload`` refuses a
        record it cannot fully trust -- in both directions the safe answer is to
        leave the process alone and just drop the record.

        The tree, not the leaf: the worker is a supervisor, and the backup or
        migration doing the side effects is its child -- which is also why an empty
        pid is not the end of the question. ``reap_orphaned_process_tree`` owns that
        walk (the leader, then the group it left behind) so both worker lanes answer
        it the same way.

        THE PASS COMMUNICATES BY WRITING, and that is why it returns nothing. Only a
        proven death retires the record; every maybe keeps it, and ``recover_processing``
        -- which runs next -- leaves any fire still carrying one ``running``. Clearing
        on a maybe throws away the ONLY durable handle on a backup or deployment that is
        still writing, and it is unrecoverable: that row is where the identity lives, so
        no later start can find the process even in principle.

        So EVERY FAILURE HERE MUST LEAVE THE RECORD ALONE, including a failure to look.
        Returning a "nothing to protect" answer on a read error was the same category
        mistake one layer up: it licensed the settle pass to strip every live worker's
        name. A row unexamined for any reason keeps its record, hence its ``running``
        status, and ``_MAX_COMMAND_WORKER_REAP_ATTEMPTS`` stops the retries from
        becoming a run that is ``running`` forever.
        """

        try:
            workers = self.request_store.list_running_command_workers()
        except Exception:
            # Warning, not debug: nothing was killed and nothing was cleared, so any
            # orphan from the last life survives this start untouched -- worth a line in
            # the log, and safe, because the unread records keep their rows ``running``.
            logger.warning(
                "failed to list command workers for recovery; leaving their records "
                "in place for the next start",
                exc_info=True,
            )
            return
        for worker in workers:
            run_id = str(worker.get("run_id") or "")
            payload = worker.get("identity")
            pid = payload.get("pid") if isinstance(payload, dict) else None
            expected = (
                process_identity_from_payload(payload, pid)
                if isinstance(pid, int) and not isinstance(pid, bool)
                else None
            )
            # An untrusted payload counts as gone: every kill path refuses a record it
            # cannot fully trust, on this pass and on every future one, so keeping it
            # would pin the run ``running`` without ever buying a kill.
            try:
                outcome = (
                    reap_orphaned_process_tree(
                        logger,
                        f"scheduled command worker {run_id}",
                        expected_identity=expected,
                    )
                    if expected is not None
                    else "gone"
                )
            except Exception:
                # One row's inspection blowing up must not abort the loop: the rows
                # after it would go unexamined AND the settle pass would never run, so
                # every other interrupted run of every other type would stay ``running``.
                logger.warning(
                    "failed to inspect command worker from run %s; treating it as "
                    "possibly alive",
                    run_id,
                    exc_info=True,
                )
                outcome = "unconfirmed"
            if outcome == "unconfirmed" and run_id and self._retain_unreaped_command_worker(run_id, payload):
                continue
            if run_id:
                self._clear_command_worker(run_id)

    def _retain_unreaped_command_worker(
        self,
        run_id: str,
        payload: Any,
    ) -> bool:
        """Keep an unkillable worker's identity for the next start, up to a limit.

        ``True`` means the record was kept and the run must stay ``running`` for
        ``list_running_command_workers`` to find it again. ``False`` is the give-up
        answer, and it has exactly two grounds: the attempts are spent, or the row is
        no longer ``running`` and so cannot hold a record at all. Then the run settles
        and reports like any other interrupted fire rather than being held open by a
        process that never exits.

        A WRITE THAT FAILS IS NOT A GROUND. The caller's next line clears the record,
        which is the one outcome this whole path exists to prevent: a locked database or
        a lost connection during the re-stamp says nothing about the process, and
        answering ``False`` there would hand a still-running backup's only durable name
        to the very code that deletes it. The already-stored record survives the failed
        write untouched -- it simply keeps the attempt count it had, so this pass is not
        charged against the cap and the next start looks again.
        """

        identity = dict(payload) if isinstance(payload, dict) else {}
        attempts = identity.get(COMMAND_WORKER_REAP_ATTEMPTS_KEY)
        attempts = attempts + 1 if isinstance(attempts, int) and not isinstance(attempts, bool) else 1
        if attempts >= self._MAX_COMMAND_WORKER_REAP_ATTEMPTS:
            logger.error(
                "giving up on command worker pid=%s from run %s after %s attempts; "
                "it may still be running",
                identity.get("pid"),
                run_id,
                attempts,
            )
            return False
        identity[COMMAND_WORKER_REAP_ATTEMPTS_KEY] = attempts
        logger.warning(
            "command worker pid=%s from run %s survived teardown; retrying on the "
            "next start (attempt %s)",
            identity.get("pid"),
            run_id,
            attempts,
        )
        try:
            # The already-serialized payload, re-stamped with its attempt count:
            # ``_require_command_worker_record`` takes a live identity and this record's
            # process is precisely the one we could not inspect our way back to.
            return bool(self.request_store.record_command_worker(run_id, identity))
        except Exception:
            logger.warning(
                "failed to re-stamp the attempt count on the command worker record for "
                "run %s; keeping the record as stored",
                run_id,
                exc_info=True,
            )
            return True

    def _command_fire_definition_id(self, request: TaskExecutionRequest) -> Optional[str]:
        """The definition id when this request is a command fire, else ``None``."""

        if request.request_type not in {"task_run", "scheduled"}:
            return None
        task_id = request.task_id
        if not task_id:
            return None
        task = self.store.get_task(task_id)
        if task is None or not task.has_command:
            return None
        return task_id

    def _command_definition_has_live_worker(self, task_id: str) -> bool:
        """Whether a previous fire of this definition may still be running.

        SINGLE FLIGHT PER DEFINITION IS THE RULE (see ``_execution_lock_key``), and
        ``self._inflight_sessions`` only enforces it for as long as this process lives.
        A crash is exactly when it stops being enforced and exactly when a survivor is
        most likely: the supervisor's tree outlives the service, its record is retained
        by ``_reap_orphaned_command_workers`` precisely because it could not be shown
        dead, and the next tick of the same cron would then start a SECOND copy of that
        backup or migration -- two writers over one dataset, which is worse than a late
        fire in every case.

        A LOOK, NEVER A SIGNAL: the retained record's whole purpose is to let a later
        start kill that tree, so a fire must not resolve the collision by killing the
        earlier copy. ``orphaned_process_tree_presence`` answers on identity and returns
        the three answers apart, and ``present``/``unknown`` both defer the new fire --
        proceeding on "could not tell" is the same wager as proceeding on "yes".

        Proof of death releases the definition here rather than waiting for the next
        start: the record is retired, the interrupted fire is SETTLED (nothing else will
        ever come back for it -- see ``_settle_recovered_command_fire``) and the new fire
        runs, so a supervisor that ended while the service was down does not hold its own
        schedule shut.

        Untrusted records do not block. Every kill path refuses them, so no pass will
        ever act on one; treating it as a live worker would wedge the definition for
        good, which is the mirror of the reap's own reading of the same payload.
        """

        try:
            workers = self.request_store.list_running_command_workers()
        except Exception:
            # Unreadable, so unknown -- and unknown defers. Deferring costs a late fire
            # that the next tick retries; guessing "clear" costs a second writer.
            logger.warning(
                "failed to check for a live command worker on task %s; deferring the "
                "fire",
                task_id,
                exc_info=True,
            )
            return True
        for worker in workers:
            if str(worker.get("definition_id") or "") != task_id:
                continue
            run_id = str(worker.get("run_id") or "")
            if run_id and run_id in self._inflight_executions:
                # This process's own fire, already covered by the in-memory lock the
                # callers check first; not an orphan and not this method's business.
                continue
            payload = worker.get("identity")
            pid = payload.get("pid") if isinstance(payload, dict) else None
            expected = (
                process_identity_from_payload(payload, pid)
                if isinstance(pid, int) and not isinstance(pid, bool)
                else None
            )
            if expected is None:
                continue
            try:
                presence = orphaned_process_tree_presence(
                    logger,
                    f"scheduled command worker {run_id}",
                    expected_identity=expected,
                )
            except Exception:
                logger.warning(
                    "failed to inspect command worker from run %s; deferring the fire "
                    "for task %s",
                    run_id,
                    task_id,
                    exc_info=True,
                )
                return True
            if presence == "gone":
                if run_id:
                    self._settle_recovered_command_fire(run_id)
                continue
            logger.warning(
                "task %s still has a command worker pid=%s from run %s (%s); deferring "
                "this fire",
                task_id,
                expected.pid,
                run_id,
                presence,
            )
            return True
        return False

    def _settle_recovered_command_fire(self, run_id: str) -> None:
        """Close the fire whose retained worker has just been shown dead.

        NO OTHER PASS COMES BACK FOR THIS ROW. ``recover_processing`` runs once, at
        startup, and deliberately steps over a run that still names a worker; the only
        other pass over stale rows -- ``sweep_stale_runs`` -- classifies orphans as
        ``run_type == "agent_run"`` and never looks at a command fire. So the row here,
        kept ``running`` by ``_reap_orphaned_command_workers`` because the process could
        not be proven dead and proven dead only now, has nobody left to close it.
        Releasing the definition without closing it would just trade a stuck schedule for
        a run that reads ``running`` until the next restart, describing a backup that
        ended long ago.

        THE RECORD GOES FIRST, because it is readable only while the row is ``running``:
        clearing after the settle would leave a dead process's name stamped on a terminal
        row where nothing would ever read or retire it. Neither order can cost a live
        process its only handle -- reaching this method IS the proof that there is no
        live process.

        Reported as ``restarted`` because that is what happened: the service died
        mid-fire and the supervisor did not outlive it. It is the same sentence the
        startup pass writes for every other run that restart cut off, so one
        interruption is not told two ways depending on which pass could prove it. The
        settle owes the notice, hence the drain nudge -- the user hears that the fire was
        interrupted, rather than only seeing the next one succeed.

        Settled through ``settle_recovered_run`` rather than the guarded writer directly,
        because the leak is not SQLite's alone: the legacy file store has no guarded
        per-run write, and skipping it here would release the definition while its
        processing file still read ``running``. The store owns that difference, so this
        method has one path instead of a supported-store branch that silently does
        nothing.

        Best-effort by construction: the caller is a fire's own admission check, and a
        store hiccup while closing the PREVIOUS run must not stop this one. Nothing is
        lost by deferring -- the record is already gone, so the next start's
        ``recover_processing`` settles the row it left behind.
        """

        self._clear_command_worker(run_id)
        settled_by = SETTLED_BY_RESTARTED
        try:
            settled = self.request_store.settle_recovered_run(
                run_id,
                interruption_error=self._t(SETTLEMENT_I18N_KEYS[settled_by]),
                cancellation_error=self._t(SETTLEMENT_I18N_KEYS[SETTLED_BY_STOPPED]),
                interrupt_reason=settled_by,
            )
            if settled is None:
                logger.info(
                    "Interrupted command fire %s was already settled elsewhere",
                    run_id,
                )
                return
            logger.warning(
                "Command fire %s settled %s after its worker was shown gone (%s)",
                run_id,
                settled,
                settled_by,
            )
            self._project_terminal_definition_result(
                self.request_store.get_run(run_id),
                execution_id=run_id,
                expected_status=settled,
            )
        except Exception:
            logger.warning(
                "failed to settle interrupted command fire %s after its worker was "
                "shown gone; leaving it for the next start",
                run_id,
                exc_info=True,
            )
            return
        self._wake_runtime_work(
            RuntimeWorkLane.REQUESTS,
            RuntimeWorkLane.RUN_CALLBACKS,
            RuntimeWorkLane.FAILURE_NOTICES,
        )

    def _execution_interruption(self, execution_id: str) -> str:
        return self._inflight_cancellation_causes.get(
            execution_id,
            SETTLED_BY_INTERRUPTED,
        )

    @staticmethod
    def _activity_run_ids(activity: Any) -> list[str]:
        run_ids: list[str] = []
        primary = str(getattr(activity, "run_id", "") or "").strip()
        if primary:
            run_ids.append(primary)
        metadata = getattr(activity, "metadata", None) or {}
        values = metadata.get("run_ids") if isinstance(metadata, dict) else None
        if isinstance(values, list):
            for value in values:
                run_id = str(value or "").strip()
                if run_id and run_id not in run_ids:
                    run_ids.append(run_id)
        return run_ids

    def _activity_registry(self) -> Any:
        return getattr(getattr(self.controller, "agent_service", None), "activities", None)

    def _activity_output_grace_seconds(self, backend: str) -> float:
        agents = getattr(getattr(self.controller, "agent_service", None), "agents", {})
        agent = agents.get(str(backend)) if isinstance(agents, dict) else None
        try:
            return max(
                0.0,
                float(getattr(agent, "ACTIVITY_OUTPUT_FLUSH_GRACE_SECONDS", 0.0)),
            )
        except (TypeError, ValueError):
            return 0.0

    def _recover_activity_lifecycle(self) -> None:
        """Reconcile persisted Activity blockers before queued-Run recovery."""

        registry = self._activity_registry()
        drain_terminals = getattr(registry, "drain_recovered_terminals", None)
        ack_terminal = getattr(registry, "ack_recovered_terminal", None)
        has_pending_output = getattr(registry, "has_pending_run_output", None)
        if callable(drain_terminals):
            for activity in drain_terminals():
                try:
                    self.settle_activity_runs(activity)
                except Exception:
                    self._pending_recovered_activity_terminals.append(activity)
                    logger.warning(
                        "Failed to settle recovered terminal Activity %s during startup",
                        getattr(activity, "id", ""),
                        exc_info=True,
                    )
                    continue
                if callable(has_pending_output) and any(
                    has_pending_output(run_id)
                    for run_id in self._activity_run_ids(activity)
                ):
                    self._pending_recovered_activity_terminals.append(activity)
                    continue
                try:
                    if callable(ack_terminal):
                        ack_terminal(activity)
                except Exception:
                    self._pending_recovered_activity_terminals.append(activity)
                    logger.warning(
                        "Failed to acknowledge recovered terminal Activity %s during startup",
                        getattr(activity, "id", ""),
                        exc_info=True,
                    )

        has_blocker = getattr(registry, "has_blocking_run_activity", None)
        for run in self.request_store.list_deferred_runs():
            run_id = str(run.get("id") or "").strip()
            if not run_id:
                continue
            if callable(has_blocker) and has_blocker(run_id):
                continue
            if callable(has_pending_output) and has_pending_output(run_id):
                continue
            if self.request_store.settle_deferred_run(run_id):
                self._wake_runtime_work(
                    RuntimeWorkLane.REQUESTS,
                    RuntimeWorkLane.RUN_CALLBACKS,
                    RuntimeWorkLane.FAILURE_NOTICES,
                )

    def _settle_pending_recovered_activity_terminals(self) -> None:
        """Acknowledge terminal snapshots only after owned output leaves the Outbox."""

        if not self._pending_recovered_activity_terminals:
            return
        registry = self._activity_registry()
        has_pending_output = getattr(registry, "has_pending_run_output", None)
        ack_terminal = getattr(registry, "ack_recovered_terminal", None)
        remaining: list[Any] = []
        for activity in self._pending_recovered_activity_terminals:
            if callable(has_pending_output) and any(
                has_pending_output(run_id)
                for run_id in self._activity_run_ids(activity)
            ):
                remaining.append(activity)
                continue
            try:
                self.settle_activity_runs(activity)
                if callable(ack_terminal):
                    ack_terminal(activity)
            except Exception:
                remaining.append(activity)
                logger.warning(
                    "Failed to settle recovered terminal Activity %s",
                    getattr(activity, "id", ""),
                    exc_info=True,
                )
        self._pending_recovered_activity_terminals = remaining

    def _settle_activity_without_output(self, activity: Any) -> None:
        """Finish an Activity Run without manufacturing user-visible text."""

        for run_id in self._activity_run_ids(activity):
            self.request_store.defer_run_terminal(
                run_id,
                terminal_status="succeeded",
            )
            if self.request_store.settle_deferred_run(run_id):
                self._wake_runtime_work(
                    RuntimeWorkLane.REQUESTS,
                    RuntimeWorkLane.RUN_CALLBACKS,
                    RuntimeWorkLane.FAILURE_NOTICES,
                )

    async def _deliver_recovered_activity_output(self, activity: Any) -> None:
        registry = self._activity_registry()
        summary = str((getattr(activity, "metadata", None) or {}).get("summary") or "").strip()
        session_id = str(getattr(activity, "session_id", "") or "").strip()
        if not strip_silent_blocks(summary).strip() or not session_id:
            self._settle_activity_without_output(activity)
            registry.ack_completed_output(activity)
            return

        try:
            target = resolve_session_id_target(session_id)
        except ValueError:
            logger.info(
                "Recovered Activity %s has no live Session route; settling without output",
                getattr(activity, "id", ""),
            )
            self._settle_activity_without_output(activity)
            registry.ack_completed_output(activity)
            return

        delivery_target = target.session_key
        delivery_key = str(
            (getattr(activity, "metadata", None) or {}).get("delivery_key_external")
            or ""
        ).strip()
        if delivery_key and delivery_key != target.session_key.to_key():
            delivery_target = parse_session_key(delivery_key)
            if delivery_target.platform != target.session_key.platform:
                raise ValueError("recovered Activity delivery target changed platform")

        context = await self._build_context(
            target.session_key,
            delivery_target=delivery_target,
            execution_id=f"activity:{getattr(activity, 'backend', '')}:{getattr(activity, 'id', '')}",
            trigger_kind="activity_recovery",
            session_id=session_id,
            agent_name=target.agent_name,
            target_info=target,
            metadata={
                "source_kind": "activity_recovery",
                "source_actor": getattr(activity, "id", None),
            },
        )
        message_id = await self.controller.emit_agent_message(
            context,
            "result",
            summary,
            output=activity_completion_output(
                activity,
                detached=True,
                completes_turn=False,
            ),
        )
        if message_id is None:
            raise RuntimeError("recovered Activity output was not persisted or delivered")
        registry.ack_completed_output(activity)

    async def _drain_recovered_activity_outputs(self) -> None:
        registry = self._activity_registry()
        runtimes = getattr(registry, "recovered_output_runtimes", None)
        claim = getattr(registry, "claim_completed_output", None)
        if not callable(runtimes) or not callable(claim):
            return
        for backend, runtime_key in runtimes():
            while True:
                activity = claim(backend, runtime_key, recovered_only=True)
                if activity is None:
                    break
                try:
                    await self._deliver_recovered_activity_output(activity)
                except Exception:
                    registry.requeue_completed_output(activity, recovered=True)
                    logger.warning(
                        "Failed to deliver recovered Activity output %s",
                        getattr(activity, "id", ""),
                        exc_info=True,
                    )
                    break
        self._settle_pending_recovered_activity_terminals()

    async def _process_recovered_activity_output(
        self,
        backend: str,
        runtime_key: str,
    ) -> bool:
        registry = self._activity_registry()
        claim = getattr(registry, "claim_completed_output", None)
        if registry is None or not callable(claim):
            return True
        activity = claim(backend, runtime_key, recovered_only=True)
        if activity is None:
            self._settle_pending_recovered_activity_terminals()
            return not bool(registry.has_completed_output(backend, runtime_key))
        try:
            await self._deliver_recovered_activity_output(activity)
        except Exception:
            registry.requeue_completed_output(activity, recovered=True)
            logger.warning(
                "Failed to deliver recovered Activity output %s",
                getattr(activity, "id", ""),
                exc_info=True,
            )
            return False
        self._settle_pending_recovered_activity_terminals()
        has_recovered_output = getattr(registry, "has_recovered_output", None)
        if callable(has_recovered_output) and has_recovered_output(
            backend,
            runtime_key,
        ):
            self._wake_runtime_work(
                RuntimeWorkLane.ACTIVITY_OUTPUTS,
                reset_cursor=True,
            )
        return True

    def validate_platform(self, platform: str) -> None:
        # The real IM platforms have a settings manager; ``avibe`` (the web
        # workbench) is a virtual platform with an IM client but no settings
        # manager — accept it too so scheduled tasks/watches can target a
        # workbench session (they fire like a harness turn, reply via message.new).
        if (
            platform not in self.controller.platform_settings_managers
            and platform not in getattr(self.controller, "im_clients", {})
        ):
            raise ValueError(f"unsupported task platform: {platform}")

    def start(self) -> None:
        if self._running:
            return
        teardown = self._service_teardown_task
        if teardown is not None:
            if not teardown.done():
                raise RuntimeError("scheduled task service generation is still stopping")
            teardown.result()
            self._service_teardown_task = None
        if self._supports_runtime_work_lanes():
            self.register_controller_runtime_work_lanes()
            self._register_runtime_work_lanes()
        else:
            self._register_request_recovery()
        try:
            self.scheduler.start()
        except BaseException:
            self._begin_stop()
            raise
        self._running = True
        if not self._supports_runtime_work_lanes():
            self._spawn_watch_store()
        else:
            self._start_legacy_runtime_work_probes()
        try:
            self.reconcile_jobs()
        except Exception as exc:
            logger.error("Initial scheduled task reconcile failed: %s", exc, exc_info=True)
        try:
            # Startup integrity check: a broken binding is otherwise invisible until
            # the next fire, which for a weekly cron is a week of silence.
            self.audit_definition_bindings()
        except Exception as exc:
            logger.error("Harness definition binding audit failed: %s", exc, exc_info=True)

    def _spawn_watch_store(self) -> None:
        self._reconcile_task = asyncio.create_task(self._watch_store())
        self._reconcile_task.add_done_callback(self._on_watch_store_done)

    def _on_watch_store_done(self, task: "asyncio.Task[Any]") -> None:
        # Only respawn if the service is still meant to be running. During
        # stop() we deliberately cancel the task and clear _running first.
        if not self._running:
            return
        if task.cancelled():
            cause: Any = "CancelledError"
        else:
            cause = task.exception()
        self._watch_store_restart_count += 1
        logger.error(
            "Scheduled task watch store exited unexpectedly "
            "(restart_count=%d, cause=%r); respawning",
            self._watch_store_restart_count,
            cause,
        )
        self._spawn_watch_store()

    def _current_asyncio_task(self) -> Optional["asyncio.Task[Any]"]:
        try:
            return asyncio.current_task()
        except RuntimeError:
            return None

    def _register_request_recovery(self) -> None:
        previous = self._request_recovery_unregistration_task
        if previous is not None:
            if not previous.done():
                raise RuntimeError(
                    "scheduled task service request recovery generation is still stopping"
                )
            previous.result()
            self._request_recovery_unregistration_task = None
        if self._request_recovery_token is not None:
            raise RuntimeError(
                "scheduled task service request recovery generation is already registered"
            )

        sqlite_store = self.request_store.sqlite_backend
        supervisor = getattr(self.controller, "runtime_work_supervisor", None)
        register = getattr(supervisor, "register", None)
        begin_unregister = getattr(supervisor, "begin_unregister", None)
        if sqlite_store is None or not callable(register) or not callable(begin_unregister):
            return
        self._request_recovery_token = register(
            RuntimeWorkLane.REQUESTS,
            FallbackRequestRecoveryHandler(
                sqlite_store,
                live_claims=lambda: self._live_claimed_run_ids,
                partition_key=self._fallback_request_partition_key,
            ),
        )

    def _supports_runtime_work_lanes(self) -> bool:
        supervisor = getattr(self.controller, "runtime_work_supervisor", None)
        return callable(getattr(supervisor, "notify", None))

    def _register_runtime_work_lanes(self) -> None:
        previous = self._request_recovery_unregistration_task
        if previous is not None:
            if not previous.done():
                raise RuntimeError(
                    "scheduled task service runtime work generation is still stopping"
                )
            previous.result()
            self._request_recovery_unregistration_task = None
        if self._runtime_work_tokens:
            raise RuntimeError(
                "scheduled task service runtime work generation is already registered"
            )
        supervisor = self.controller.runtime_work_supervisor
        registered: list[RuntimeWorkRegistrationToken] = []
        try:
            for lane in (
                RuntimeWorkLane.TASK_DEFINITIONS,
                RuntimeWorkLane.REQUESTS,
                RuntimeWorkLane.RUN_CALLBACKS,
                RuntimeWorkLane.VAULT_CALLBACKS,
                RuntimeWorkLane.FAILURE_NOTICES,
                RuntimeWorkLane.STALE_RUNS,
            ):
                token = supervisor.register(
                    lane,
                    _ScheduledRuntimeWorkHandler(self, lane),
                )
                self._runtime_work_tokens[lane] = token
                registered.append(token)
        except BaseException:
            for token in registered:
                supervisor.begin_unregister(token)
            self._runtime_work_tokens.clear()
            raise
        self._request_recovery_token = self._runtime_work_tokens[
            RuntimeWorkLane.REQUESTS
        ]

    def register_controller_runtime_work_lanes(
        self,
    ) -> tuple[RuntimeWorkRegistrationToken, ...]:
        """Register live-producer lanes for the full controller generation."""

        if not self._supports_runtime_work_lanes():
            return ()
        existing = self._controller_runtime_work_tokens.get(
            RuntimeWorkLane.ACTIVITY_OUTPUTS
        )
        if existing is not None:
            return ()
        token = self.controller.runtime_work_supervisor.register(
            RuntimeWorkLane.ACTIVITY_OUTPUTS,
            _ScheduledRuntimeWorkHandler(self, RuntimeWorkLane.ACTIVITY_OUTPUTS),
        )
        self._controller_runtime_work_tokens[RuntimeWorkLane.ACTIVITY_OUTPUTS] = token
        return (token,)

    def _begin_runtime_work_unregistration(self) -> asyncio.Task[None] | None:
        if not self._runtime_work_tokens:
            return self._begin_request_recovery_unregistration()
        existing = self._request_recovery_unregistration_task
        if existing is not None:
            return existing
        supervisor = self.controller.runtime_work_supervisor
        tasks = [
            supervisor.begin_unregister(token)
            for token in self._runtime_work_tokens.values()
        ]
        self._runtime_work_tokens.clear()
        self._request_recovery_token = None

        async def _join() -> None:
            await asyncio.gather(*tasks)

        joined = asyncio.create_task(
            _join(),
            name="scheduled-runtime-work-unregister",
        )
        self._request_recovery_unregistration_task = joined
        return joined

    def _wake_runtime_work(
        self,
        *lanes: RuntimeWorkLane,
        reset_cursor: bool = False,
    ) -> None:
        controller = getattr(self, "controller", None)
        supervisor = getattr(controller, "runtime_work_supervisor", None)
        notify = getattr(supervisor, "notify", None)
        if callable(notify):
            if reset_cursor:
                notify(*lanes, reset_cursor=True)
            else:
                notify(*lanes)
        else:
            self._drain_dirty = True

    def _schedule_runtime_work_wake(
        self,
        lane: RuntimeWorkLane,
        delay: float,
    ) -> None:
        supervisor = getattr(self.controller, "runtime_work_supervisor", None)
        notify_after = getattr(supervisor, "notify_after", None)
        token = self._runtime_work_tokens.get(
            lane,
            self._controller_runtime_work_tokens.get(lane),
        )
        if callable(notify_after) and token is not None:
            notify_after(token, delay)

    async def _run_runtime_sync(self, operation, /, *args, **kwargs):  # noqa: ANN001, ANN202
        call = partial(operation, *args, **kwargs)
        supervisor = getattr(self.controller, "runtime_work_supervisor", None)
        run_sync = getattr(supervisor, "run_sync", None)
        if callable(run_sync):
            return await run_sync(call)
        return await asyncio.to_thread(call)

    def _start_legacy_runtime_work_probes(self) -> None:
        if self.store.sqlite_backend is None:
            self._legacy_task_signature = _path_signature(self.store.path)
            self._legacy_probe_tasks["tasks"] = asyncio.create_task(
                self._legacy_task_definition_probe(),
                name="scheduled-runtime-work-task-file-probe",
            )
        if self.request_store.sqlite_backend is None:
            self._legacy_request_signature = self.request_store._state_signature()
            self._legacy_probe_tasks["requests"] = asyncio.create_task(
                self._legacy_request_directory_probe(),
                name="scheduled-runtime-work-request-file-probe",
            )

    async def _legacy_task_definition_probe(self) -> None:
        """Wake only when the injected task file signature changes."""

        try:
            while self._running:
                await asyncio.sleep(2)
                signature = await self._run_runtime_sync(_path_signature, self.store.path)
                if signature == self._legacy_task_signature:
                    continue
                self._legacy_task_signature = signature
                self._wake_runtime_work(RuntimeWorkLane.TASK_DEFINITIONS)
        except asyncio.CancelledError:
            raise

    async def _legacy_request_directory_probe(self) -> None:
        """Wake every Run consumer on a legacy directory edge."""

        try:
            while self._running:
                await asyncio.sleep(2)
                signature = await self._run_runtime_sync(
                    self.request_store._state_signature
                )
                if signature == self._legacy_request_signature:
                    continue
                self._legacy_request_signature = signature
                self._wake_runtime_work(
                    RuntimeWorkLane.REQUESTS,
                    RuntimeWorkLane.RUN_CALLBACKS,
                    RuntimeWorkLane.STALE_RUNS,
                )
        except asyncio.CancelledError:
            raise

    def _begin_request_recovery_unregistration(
        self,
    ) -> asyncio.Task[None] | None:
        existing = self._request_recovery_unregistration_task
        if existing is not None:
            return existing
        token = self._request_recovery_token
        if token is None:
            return None
        supervisor = getattr(self.controller, "runtime_work_supervisor", None)
        begin_unregister = getattr(supervisor, "begin_unregister", None)
        if not callable(begin_unregister):
            return None

        # The supervisor invalidates synchronously before returning the join task.
        # A restart must wait for that exact generation to finish joining workers.
        task = begin_unregister(token)
        self._request_recovery_token = None
        self._request_recovery_unregistration_task = task
        return task

    async def _settle_service_teardown_owners(self) -> None:
        manager = getattr(self.controller, "session_turns", None)
        release_durable = getattr(manager, "release_for_service_shutdown", None)
        if not callable(release_durable):
            return
        try:
            await release_durable()
        except Exception:
            logger.exception(
                "Failed to settle durable Session Turns during service shutdown",
            )

    def _ensure_service_teardown_task(self) -> Optional["asyncio.Task[None]"]:
        if self._service_teardown_task is not None:
            return self._service_teardown_task
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        self._service_teardown_task = loop.create_task(
            self._settle_service_teardown_owners()
        )
        return self._service_teardown_task

    def _shutdown_interruption_for(self, request_id: str) -> str:
        try:
            run = self.request_store.get_run(request_id)
        except Exception:
            logger.exception(
                "Failed to read Run %s before service shutdown cancellation",
                request_id,
            )
            return SETTLED_BY_RESTARTED
        if run is not None and bool(run.get("cancel_requested")):
            return SETTLED_BY_STOPPED
        return SETTLED_BY_RESTARTED

    def _is_command_execution(self, request: TaskExecutionRequest) -> bool:
        """Is this in-flight request a command fire rather than an Agent turn?

        Answered from the REQUEST, which is fixed for the life of the fire, before
        falling back to the live definition. Asking the store first got this wrong in
        the one case where the answer decides whether a child process lives: removing
        a task drops the definition and leaves its Run in flight, so a user cancelling
        a misbehaving command was told it was not a command and the child ran on to
        its ``--timeout``. The fallback still covers runs enqueued before the snapshot
        existed, whose definition is the only record there is.
        """

        if request.request_type not in {"task_run", "scheduled"} or not request.task_id:
            return False
        if command_snapshot_of_run({"metadata": request.metadata}) is not None:
            return True
        task = self.store.get_task(request.task_id)
        return bool(task is not None and task.has_command)

    def _propagate_requested_cancellations(self) -> None:
        """Pull the trigger on in-flight COMMAND fires the user has cancelled.

        ``cancel_run`` sets a flag; for a command fire nothing was reading it while the
        work was in flight. The flag was honoured only at settlement, and for a command
        that hangs that is up to ``--timeout`` away -- six hours by default -- so the
        child kept running, kept holding whatever it held, and the row the user had just
        cancelled reported a run that was over. Every other lane already closes this
        loop: a queued claim is terminalized at the door by ``claim_pending_run``, and a
        turn is interrupted by the turn lane.

        The kill itself needs nothing new. ``run_supervised_command`` turns a cancelled
        await into a process-tree teardown, and ``_execute_claimed_request`` settles the
        row from its ``CancelledError`` handler -- before the result stamp, so a
        cancelled fire also queues no escalation and stamps no failure on the definition.
        What was missing was somebody to observe the flag, which is this: the same
        ``_inflight_cancellation_causes`` + ``task.cancel()`` pair service shutdown uses,
        with the user named as the cause so the run reads ``stopped`` rather than
        ``interrupted``.

        COMMAND FIRES ONLY, deliberately. For an Agent turn cancelling this coroutine
        would abandon work that is still streaming into a conversation, and stopping a
        turn coherently -- interrupting the backend, closing the transcript -- belongs to
        the lane that owns it. A command's coroutine cancel IS the complete stop.
        """

        if not self._inflight_executions:
            return
        for request_id, execution in list(self._inflight_executions.items()):
            if execution.done() or request_id in self._inflight_cancellation_causes:
                continue
            request = self._inflight_requests.get(request_id)
            if request is None or not self._is_command_execution(request):
                continue
            try:
                run = self.request_store.get_run(request_id)
            except Exception:
                logger.exception(
                    "Failed to read Run %s while checking for a requested cancellation",
                    request_id,
                )
                continue
            if not run or not bool(run.get("cancel_requested")):
                continue
            if str(run.get("status") or "") in TERMINAL_RUN_STATUSES:
                continue
            logger.info(
                "Stopping in-flight command Run %s: cancellation requested", request_id
            )
            self._inflight_cancellation_causes[request_id] = SETTLED_BY_STOPPED
            execution.cancel()

    async def _propagate_requested_cancellations_async(self) -> None:
        """Off-loop store reads with loop-owned cancellation decisions."""

        candidates = [
            (request_id, execution, self._inflight_requests.get(request_id))
            for request_id, execution in self._inflight_executions.items()
            if not execution.done()
            and request_id not in self._inflight_cancellation_causes
        ]
        for request_id, execution, request in candidates:
            if request is None or not self._is_command_execution(request):
                continue
            try:
                run = await self._run_runtime_sync(
                    self.request_store.get_run,
                    request_id,
                )
            except Exception:
                logger.exception(
                    "Failed to read Run %s while checking for a requested cancellation",
                    request_id,
                )
                continue
            if (
                not run
                or not bool(run.get("cancel_requested"))
                or str(run.get("status") or "") in TERMINAL_RUN_STATUSES
                or execution.done()
            ):
                continue
            self._inflight_cancellation_causes[request_id] = SETTLED_BY_STOPPED
            execution.cancel()

    def _begin_stop(
        self,
        *,
        cancel_reconcile: bool = True,
    ) -> asyncio.Task[None] | None:
        self._running = False
        request_recovery_stop = self._begin_runtime_work_unregistration()
        current_task = self._current_asyncio_task()
        if cancel_reconcile and self._reconcile_task and self._reconcile_task is not current_task:
            self._reconcile_task.cancel()
        for probe in self._legacy_probe_tasks.values():
            if probe is not current_task:
                probe.cancel()
        # Cancelled by name, and not gated on ``cancel_reconcile``: the notice drain is
        # no longer part of the watch coroutine, so stopping the watch leaves it running
        # — a delivery on behalf of a service this process no longer owns, or one that
        # outlives shutdown. The identity check is the only exemption it needs.
        if self._notice_drain_task and self._notice_drain_task is not current_task:
            self._notice_drain_task.cancel()
        for request_id, task in list(self._inflight_executions.items()):
            if task is not current_task:
                self._inflight_cancellation_causes[request_id] = (
                    self._shutdown_interruption_for(request_id)
                )
                task.cancel()
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            logger.debug("Failed to shut down scheduler", exc_info=True)
        self._ensure_service_teardown_task()
        return request_recovery_stop

    def _owns_service_instance(self) -> bool:
        if not self._requires_service_lease:
            return True
        if runtime.current_process_owns_service_instance():
            return True
        logger.error(
            "Scheduled task service lost the service lock; requesting controller shutdown"
        )
        self._running = False
        request_shutdown = getattr(self.controller, "request_shutdown", None)
        if callable(request_shutdown):
            # Lease loss is process-wide. A service-local teardown would settle
            # durable Turns and cancel their executor wrappers while backend
            # receivers remain alive, splitting Run/Turn ownership from the
            # native runtime. Let the controller stop all runtime owners as one
            # generation instead.
            request_shutdown("service lease lost")
        else:
            # Preserve standalone/lightweight controller behavior.
            self._begin_stop()
        return False

    async def stop(self) -> None:
        request_recovery_stop = self._begin_stop()
        legacy_probes = tuple(self._legacy_probe_tasks.values())
        self._legacy_probe_tasks.clear()
        if legacy_probes:
            await asyncio.gather(*legacy_probes, return_exceptions=True)
        if self._reconcile_task:
            self._reconcile_task.cancel()
            try:
                await self._reconcile_task
            except asyncio.CancelledError:
                pass
            self._reconcile_task = None
        # And the dispatched notice drain, awaited rather than merely cancelled: a
        # delivery suspended in a transport send has to be given the chance to unwind
        # before the process leaves, or shutdown races a coroutine that is still
        # writing to the notice row it claimed.
        if self._notice_drain_task:
            self._notice_drain_task.cancel()
            try:
                await self._notice_drain_task
            except (asyncio.CancelledError, Exception):
                pass
            self._notice_drain_task = None
        # Await each exact in-flight execution so its guarded terminal write lands
        # before shutdown returns. Queued requests were never claimed and remain
        # queued for the next start.
        inflight = list(self._inflight_executions.values())
        for task in inflight:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        teardown = self._ensure_service_teardown_task()
        if teardown is not None:
            await teardown
        if request_recovery_stop is None:
            request_recovery_stop = self._begin_request_recovery_unregistration()
        if request_recovery_stop is not None:
            await request_recovery_stop
            if self._request_recovery_unregistration_task is request_recovery_stop:
                self._request_recovery_unregistration_task = None
        self._inflight_executions.clear()
        self._request_capacity_reservations.clear()
        self._live_claimed_run_ids = frozenset()
        self._inflight_requests.clear()
        self._inflight_cancellation_causes.clear()
        self._inflight_sessions.clear()
        self._session_lock_owners.clear()

    async def _watch_store(self) -> None:
        while self._running:
            if not self._owns_service_instance():
                return
            try:
                await self._drain_recovered_activity_outputs()
                store_changed = self.store.maybe_reload()
                request_store_changed = self.request_store.maybe_reload()
                should_drain = store_changed or request_store_changed or self._drain_dirty
                if store_changed:
                    self.reconcile_jobs()
                if should_drain:
                    self._drain_dirty = False
                    try:
                        await self._drain_requests()
                        await self._drain_callbacks()
                    except Exception:
                        self._drain_dirty = True
                        raise
                # A failed run emits no store change anyone else notices, so — like
                # the vault and sweep passes below — only a periodic pass finds it.
                # Cheap: one indexed lookup that no-ops when nothing is owed.
                # DISPATCHED, not awaited: see ``_spawn_failure_notice_drain``.
                self._spawn_failure_notice_drain()
                # Vault requests resolve via the web/API layer, which emits no run-store change,
                # so sweep for owed auto-resume callbacks every tick — a cheap indexed lookup that
                # no-ops when nothing is pending.
                await self._drain_vault_callbacks()
                # A cancellation requested against work THIS process is running emits no
                # store change either — the flag is written by the CLI or the UI — so
                # only a periodic pass can act on it. No-ops unless a command fire is
                # actually in flight.
                self._propagate_requested_cancellations()
                # Same reason, one layer down: a run whose owner vanished emits no
                # store change either, so only a periodic pass can find it. Self
                # rate-limited, so riding this tick is cheap.
                self._sweep_stale_runs()
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Scheduled task store watch failed: %s", exc, exc_info=True)
                try:
                    await asyncio.sleep(2)
                except asyncio.CancelledError:
                    raise

    def _runtime_seconds(self, name: str, default: int) -> int:
        """Read one sweep timing knob off runtime config, tolerating junk values."""

        runtime_config = getattr(getattr(self.controller, "config", None), "runtime", None)
        try:
            return int(getattr(runtime_config, name, default))
        except (TypeError, ValueError):
            return default

    def _owned_agent_run_ids(
        self,
        *,
        reconcile_terminal: bool = True,
        candidate_run_ids: Optional[set[str]] = None,
    ) -> set[str]:
        """Every run id something in THIS process is still legitimately executing.

        Two lanes own a ``running`` row and neither can see the other:

        - the drain lane: ``_inflight_executions``, one entry per claimed request
          whose ``_execute_claimed_request`` task has not finished;
        - the turn lane: a workbench/web turn that took over out of band. Those
          never enter ``_inflight_executions``, so they are asked for directly via
          :meth:`SessionTurnManager.owned_agent_run_ids`.

        Raises when the turn lane cannot be reached. That is deliberate: the caller
        must fail closed, because a missing provider silently reads as "no live
        turns own anything", which would terminalize every streaming run.
        """

        candidates = (
            {str(run_id) for run_id in candidate_run_ids if str(run_id or "").strip()}
            if candidate_run_ids is not None
            else None
        )
        owned = set(self._inflight_executions)
        if candidates is not None:
            owned &= candidates
        session_turns = getattr(self.controller, "session_turns", None)
        provider_name = (
            "owned_agent_run_ids"
            if reconcile_terminal
            else "snapshot_owned_agent_run_ids"
        )
        provider = getattr(session_turns, provider_name, None)
        if not callable(provider):
            raise RuntimeError(f"controller.session_turns.{provider_name} is unavailable")
        provided = provider() if candidates is None else provider(candidates)
        owned |= {str(run_id) for run_id in provided if run_id}
        return owned

    def snapshot_owned_agent_run_ids(self, candidate_run_ids: set[str]) -> set[str]:
        """Expose the exact current ownership set to read-only operator views."""

        return self._owned_agent_run_ids(
            reconcile_terminal=False,
            candidate_run_ids=candidate_run_ids,
        )

    def _deliverable_queued_run_ids(self) -> set[str]:
        """Queued runs whose transport is ready RIGHT NOW, whatever the row remembers.

        The drain records ``transport_unavailable`` when it defers a run, but it
        ``break``s at ``_MAX_CONCURRENT_EXECUTIONS`` without examining the rows below
        the cut — so their stamp is never refreshed and stays true-at-the-time long
        after the platform reconnected. This is the live half of that evidence: a run
        listed here is deliverable and is only queued for capacity, so the sweep must
        leave it alone (Codex P1).
        """

        return {
            pending.id
            for pending in self.request_store.list_pending()
            if self._transport_ready_for_request(pending)
        }

    def _release_leaked_session_locks(self) -> set[str]:
        """Drop per-session locks whose owning execution no longer exists.

        A terminalized row is only half the repair. ``_inflight_sessions`` gates
        dispatch for the whole conversation, so an entry that outlives its execution
        wedges every later run for that session — the database reads honest and the
        session still never drains.

        ``_on_execution_done`` normally releases the lock, so the only way one
        survives is if that callback was never attached (``asyncio.create_task``
        raising after the lock was taken). Keying off the recorded owner rather than
        off the swept rows is what makes this safe in the other direction: a lock
        held by a live task is never freed, so this can never let two turns run
        concurrently in one session.
        """

        leaked = {
            lock_key: run_id
            for lock_key, run_id in self._session_lock_owners.items()
            if run_id not in self._inflight_executions
        }
        for lock_key, run_id in leaked.items():
            self._session_lock_owners.pop(lock_key, None)
            self._inflight_sessions.discard(lock_key)
            logger.warning(
                "Released leaked session lock %s owned by dead execution %s", lock_key, run_id
            )
        if leaked:
            # The wedge is gone; re-check the queue now instead of waiting for the
            # next store change, which a stuck session has no reason to produce.
            self._wake_runtime_work(
                RuntimeWorkLane.REQUESTS,
                RuntimeWorkLane.RUN_CALLBACKS,
                RuntimeWorkLane.FAILURE_NOTICES,
            )
        return set(leaked)

    def _sweep_stale_runs(self, *, release_leaked_locks: bool = True) -> None:
        """Terminalize runs that nothing is executing any more (plan §4).

        Rides the existing store tick because the leak it repairs is the ABSENCE of
        an event — a turn that never reported back, a transport that never came up, a
        queue gate that never reopened — so nothing will ever wake it up. Rate
        limited to ``harness_run_sweep_interval_seconds`` so the ordinary case
        (nothing stale) costs one indexed SELECT per interval, not one per tick.
        """

        interval = self._runtime_seconds(
            "harness_run_sweep_interval_seconds", DEFAULT_HARNESS_RUN_SWEEP_INTERVAL_SECONDS
        )
        if interval <= 0:
            return
        now = time.monotonic()
        # The first tick after startup sweeps immediately: a restart is exactly when
        # the previous process's orphans are sitting there waiting to be found.
        if self._last_sweep_at and now - self._last_sweep_at < interval:
            return
        self._last_sweep_at = now
        try:
            owned_run_ids = self._owned_agent_run_ids()
        except Exception:
            # Fail closed. "Nobody owns these runs" and "I cannot tell who owns them"
            # are opposite answers, and acting on the second would fail runs that are
            # still streaming. Skipping costs one interval of staleness.
            if not self._sweep_ownership_unavailable_logged:
                self._sweep_ownership_unavailable_logged = True
                logger.warning("Skipping stale-run sweep: run ownership is unknown", exc_info=True)
            else:
                logger.debug("Skipping stale-run sweep: run ownership is unknown", exc_info=True)
            return
        self._sweep_ownership_unavailable_logged = False
        queued_ttl_seconds = self._runtime_seconds(
            "harness_run_queued_ttl_seconds", DEFAULT_HARNESS_RUN_QUEUED_TTL_SECONDS
        )
        try:
            deliverable_run_ids = self._deliverable_queued_run_ids()
        except Exception:
            # Same fail-closed posture as ownership, expressed as "disable the class":
            # a recorded transport reason is only half the evidence, and without the
            # live half a deliverable run could be failed for a transport that is back.
            logger.warning(
                "Skipping transport-stale sweep: queue deliverability is unknown", exc_info=True
            )
            deliverable_run_ids = set()
            queued_ttl_seconds = 0
        swept = self.request_store.sweep_stale_runs(
            owned_run_ids=owned_run_ids,
            deliverable_run_ids=deliverable_run_ids,
            error_texts={reason: self._t(key) for reason, key in SWEEP_I18N_KEYS.items()},
            orphan_grace_seconds=self._runtime_seconds(
                "harness_run_orphan_grace_seconds", DEFAULT_HARNESS_RUN_ORPHAN_GRACE_SECONDS
            ),
            queued_ttl_seconds=queued_ttl_seconds,
        )
        # Unconditional: the in-memory wedge and the stale rows are independent
        # failures, and either can outlive the other.
        if release_leaked_locks:
            self._release_leaked_session_locks()
        if swept:
            self._wake_runtime_work(
                RuntimeWorkLane.REQUESTS,
                RuntimeWorkLane.RUN_CALLBACKS,
                RuntimeWorkLane.FAILURE_NOTICES,
            )
            self._retire_swept_queue_segments(swept)

    def _stale_run_sweep_delay_seconds(self) -> float | None:
        interval = self._runtime_seconds(
            "harness_run_sweep_interval_seconds",
            DEFAULT_HARNESS_RUN_SWEEP_INTERVAL_SECONDS,
        )
        if interval <= 0:
            return None
        if not self._last_sweep_at:
            return 0.0
        return max(0.0, interval - (time.monotonic() - self._last_sweep_at))

    def _retire_swept_queue_segments(self, swept: list[Any]) -> None:
        """Drop the persisted Workbench queue rows a swept run left behind.

        Terminalizing the row is not enough for an avibe session: the queued
        ``messages`` segment that carried the run's prompt is not reclaimed by
        ``recover_persisted_agent_run_queue`` — recovery ignores references whose run
        is no longer ``queued`` — so the Session keeps showing stale pending input
        until an unrelated user send happens to force ``flush_queue`` to retire it
        (Codex P2). Reconcile immediately from the ids the sweep already reported.

        Retirement is scoped to each run's own ``agent_run:<id>`` native id, so it can
        never touch a live sibling's row, and a per-run failure is logged and skipped
        rather than aborting the rest — an honest DB row plus one stale queue segment
        beats leaving every other session unreconciled.
        """

        touched: set[str] = set()
        for run in swept:
            session_id = str(getattr(run, "session_id", "") or "").strip()
            run_id = str(getattr(run, "run_id", "") or "").strip()
            if not session_id or not run_id:
                continue
            try:
                retired = _retire_stale_agent_run_queue_rows(
                    session_id=session_id, execution_ids=[run_id]
                )
            except Exception:
                logger.warning(
                    "sweep: failed to retire queue rows for run %s", run_id, exc_info=True
                )
                continue
            if retired:
                touched.add(session_id)
        if not touched:
            return
        try:
            from core.inbox_events import bus
        except Exception:
            logger.debug("sweep: inbox bus unavailable for queue.updated", exc_info=True)
            return
        for session_id in sorted(touched):
            try:
                bus.publish("queue.updated", {"session_id": session_id})
            except Exception:
                logger.debug("sweep: queue.updated publish failed", exc_info=True)

    def reconcile_jobs(self) -> None:
        if not self._owns_service_instance():
            return
        desired_ids = set()
        desired_job_ids = set()
        for task in self.store.list_tasks():
            if not task.enabled or task.retired_at is not None:
                continue
            desired_ids.add(task.id)
            signature = (
                task.schedule_type,
                task.cron,
                task.run_at,
                task.timezone,
                task.session_id,
                task.session_key,
                task.prompt,
                task.enabled,
                task.updated_at if task.schedule_type == "at" else None,
            )
            job_id = self._job_ids.get(task.id, task.id)
            if self._job_signatures.get(task.id) == signature and self.scheduler.get_job(job_id):
                desired_job_ids.add(job_id)
                continue
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)
            self._one_shot_job_identities.pop(job_id, None)
            try:
                trigger = self._build_trigger(task)
                job_id = (
                    f"{task.id}:at:{uuid4().hex[:12]}"
                    if task.schedule_type == "at"
                    else task.id
                )
                self.scheduler.add_job(
                    self._run_task,
                    trigger=trigger,
                    id=job_id,
                    replace_existing=True,
                    coalesce=True,
                    max_instances=1,
                    args=[
                        task.id,
                        task.run_at if task.schedule_type == "at" else None,
                        task.timezone if task.schedule_type == "at" else None,
                        task.updated_at if task.schedule_type == "at" else None,
                        job_id,
                    ],
                )
            except Exception as exc:
                self._job_signatures.pop(task.id, None)
                self._job_ids.pop(task.id, None)
                logger.error("Failed to reconcile scheduled task %s: %s", task.id, exc, exc_info=True)
                continue
            self._job_ids[task.id] = job_id
            desired_job_ids.add(job_id)
            if task.schedule_type == "at" and task.run_at:
                self._one_shot_job_identities[job_id] = (
                    task.id,
                    task.run_at,
                    task.timezone,
                    task.updated_at,
                )
            self._job_signatures[task.id] = signature

        for job in list(self.scheduler.get_jobs()):
            if job.id not in desired_job_ids:
                self.scheduler.remove_job(job.id)
                self._one_shot_job_identities.pop(job.id, None)
        for task_id in set(self._job_ids) - desired_ids:
            job_id = self._job_ids.pop(task_id)
            self._one_shot_job_identities.pop(job_id, None)
            self._job_signatures.pop(task_id, None)

    def _build_trigger(self, task: ScheduledTask):
        tz = ZoneInfo(task.timezone)
        if task.schedule_type == "cron":
            if not task.cron:
                raise ValueError(f"scheduled task {task.id} is missing cron expression")
            return CronTrigger.from_crontab(task.cron, timezone=tz)
        if task.schedule_type == "at":
            if not task.run_at:
                raise ValueError(f"scheduled task {task.id} is missing run_at timestamp")
            # Same resolver the harness payload's ``next_run_at`` uses, so the
            # time the row shows is the time this trigger fires.
            return DateTrigger(run_date=resolve_run_at(task.run_at, task.timezone))
        raise ValueError(f"unknown schedule type: {task.schedule_type}")

    def _on_scheduler_event(self, event: JobExecutionEvent) -> None:
        """Persist an APScheduler-observed one-shot misfire or callback failure.

        A clock comparison cannot tell whether the scheduler consumed a fire.
        Both paths re-assert the registered schedule identity, so an event queued
        before an edit cannot terminalize the replacement definition.
        """

        job_id = str(event.job_id)
        identity = self._one_shot_job_identities.get(job_id)
        if identity is None:
            return
        task_id, expected_run_at, expected_timezone, expected_updated_at = identity
        try:
            scheduled = resolve_run_at(expected_run_at, expected_timezone)
        except Exception:
            logger.warning(
                "Ignoring scheduler event for task %s with an invalid registered run_at",
                task_id,
                exc_info=True,
            )
            return
        if scheduled != event.scheduled_run_time:
            logger.info(
                "Ignoring stale scheduler event for task %s: event=%s registered=%s",
                task_id,
                event.scheduled_run_time,
                scheduled,
            )
            return
        changed = False
        if event.code == EVENT_JOB_ERROR:
            detail = str(event.exception or "unknown scheduler callback error")
            changed = self._recover_failed_one_shot_fire(
                task_id,
                expected_run_at=expected_run_at,
                expected_timezone=expected_timezone,
                expected_updated_at=expected_updated_at,
                expected_job_id=job_id,
                error=self._t("harness.task.schedulerCallbackFailed", detail=detail),
            )
        else:
            self.store.refresh_task(task_id)
            changed = self.store.retire_missed_one_shot(
                task_id,
                expected_run_at=expected_run_at,
                expected_timezone=expected_timezone,
                expected_updated_at=expected_updated_at,
            )
        if changed:
            self._job_signatures.pop(task_id, None)
            _publish_task_definitions_updated()
        if self._job_ids.get(task_id) == job_id:
            self._job_ids.pop(task_id, None)
            self._job_signatures.pop(task_id, None)
        self._one_shot_job_identities.pop(job_id, None)
        if not changed:
            # refresh_task above consumed any cross-process invalidation. Return
            # the refreshed desired row to the existing scheduler owner now that
            # APScheduler has removed the stale DateTrigger that raised this event.
            self.reconcile_jobs()

    def _recover_failed_one_shot_fire(
        self,
        task_id: str,
        *,
        expected_run_at: str,
        expected_timezone: str,
        expected_updated_at: str,
        expected_job_id: str,
        error: str,
    ) -> bool:
        """Atomically retire an exact fire with a failed owner Run."""

        task = self.store.refresh_task(task_id)
        if (
            task is None
            or not task.enabled
            or task.schedule_type != "at"
            or task.run_at != expected_run_at
            or task.timezone != expected_timezone
            or task.updated_at != expected_updated_at
        ):
            return False
        recovered = self.request_store.enqueue_task_run(
            task.id,
            source_kind="scheduler",
            task=task,
            suppress_scheduler_successor=True,
            expected_run_at=expected_run_at,
            expected_timezone=expected_timezone,
            expected_updated_at=expected_updated_at,
            expected_job_id=expected_job_id,
            terminal_error=error,
        )
        if recovered is None:
            return False
        self.store.load()
        return True

    async def _reconcile_rejected_one_shot_fire(self, task_id: str) -> None:
        """Return a rejected DateTrigger callback to the existing scheduler owner."""

        # The store invalidation may already have been consumed by the fallback
        # loop before this callback observes the stale generation. Force a fresh
        # mirror so reconciliation sees the replacement even in that ordering.
        await self._run_runtime_sync(self.store.load)
        self.reconcile_jobs()

    async def _reconcile_rejected_cron_fire(self, task_id: str) -> None:
        """Re-register the schedule after a cron generation's enqueue was refused."""

        # A refusal is either benign -- an earlier scheduler fire is still queued
        # -- or the stale-generation case this callback could not see: the mirror
        # read above may predate a cron -> ``at`` edit committed on another
        # connection, so the guard let the fire through and the storage CAS was
        # the layer that caught it. Only the second needs the scheduler, and only
        # this callback can ask for it: the cron job that keeps firing IS the
        # stale generation, so nothing else would install the DateTrigger. Force a
        # fresh mirror, then reconcile only when the definition is no longer the
        # cron this job was registered for.
        await self._run_runtime_sync(self.store.load)
        task = self.store.get_task(task_id)
        if task is None or task.schedule_type != "cron":
            self.reconcile_jobs()

    async def _run_task(
        self,
        task_id: str,
        expected_run_at: Optional[str] = None,
        expected_timezone: Optional[str] = None,
        expected_updated_at: Optional[str] = None,
        expected_job_id: Optional[str] = None,
    ) -> None:
        if not self._owns_service_instance():
            return
        if expected_job_id is not None and self._job_ids.get(task_id) != expected_job_id:
            if expected_run_at is not None:
                await self._reconcile_rejected_one_shot_fire(task_id)
            return
        task = await self._run_runtime_sync(self.store.refresh_task, task_id)
        if not task or not task.enabled:
            if expected_run_at is not None:
                await self._reconcile_rejected_one_shot_fire(task_id)
            return
        # The registration's own shape says what this callback fired for, and the
        # refreshed definition has to still be that thing. A one-shot must match
        # the exact schedule identity it registered. A cron registration must at
        # least still find a cron definition: enqueueing against a definition that
        # became a one-shot would spend a fire its run_at has not reached, and
        # would not retire it, so that same one-shot would run again later.
        if expected_run_at is not None:
            if (
                task.schedule_type != "at"
                or task.run_at != expected_run_at
                or task.timezone != expected_timezone
                or task.updated_at != expected_updated_at
            ):
                await self._reconcile_rejected_one_shot_fire(task_id)
                return
        elif expected_job_id is not None and task.schedule_type != "cron":
            # refresh_task above already consumed any invalidation; hand the
            # replacement schedule back to the scheduler that owns it.
            self.reconcile_jobs()
            return
        # Every registered job carries its APScheduler job id so the callback can
        # reject a stale generation above. Only a one-shot fire carries it further:
        # downstream the job id is one field of the schedule identity that retires
        # the definition, and the store requires that identity whole or absent. A
        # cron fire has no run_at, so it must not present a partial one.
        enqueue_job_id = expected_job_id if expected_run_at is not None else None
        try:
            queued = await self._run_runtime_sync(
                self.request_store.enqueue_task_run,
                task.id,
                source_kind="scheduler",
                task=task,
                suppress_scheduler_successor=True,
                expected_run_at=expected_run_at,
                expected_timezone=expected_timezone,
                expected_updated_at=expected_updated_at,
                expected_job_id=enqueue_job_id,
            )
        except Exception as exc:
            if expected_run_at is None or expected_job_id is None:
                raise
            error = self._t(
                "harness.task.schedulerCallbackFailed", detail=str(exc)
            )
            recovered = await self._run_runtime_sync(
                self._recover_failed_one_shot_fire,
                task.id,
                expected_run_at=expected_run_at,
                expected_timezone=str(expected_timezone),
                expected_updated_at=str(expected_updated_at),
                expected_job_id=expected_job_id,
                error=error,
            )
            if recovered:
                _publish_task_definitions_updated()
                return
            raise
        if queued is not None:
            if expected_run_at is not None:
                # The SQLite enqueue atomically retired the definition with this
                # Run. Reload the process mirror and wake open Workbench pages.
                await self._run_runtime_sync(self.store.load)
                _publish_task_definitions_updated()
            self._wake_runtime_work(RuntimeWorkLane.REQUESTS)
        elif expected_run_at is not None:
            await self._reconcile_rejected_one_shot_fire(task_id)
        elif expected_job_id is not None:
            await self._reconcile_rejected_cron_fire(task_id)

    def _request_partition_key(self, request: TaskExecutionRequest) -> str:
        lock_key = self._execution_lock_key(request)
        if lock_key:
            return lock_key
        return f"run:{request.id}"

    def _fallback_request_partition_key(self, row: dict[str, Any]) -> str:
        return self._request_partition_key(TaskExecutionRequest.from_dict(row))

    async def _process_pending_request(
        self,
        pending: TaskExecutionRequest,
    ) -> bool:
        if not self._owns_service_instance():
            return True
        if pending.id in self._request_capacity_reservations:
            return True
        if (
            len(self._inflight_executions) + len(self._request_capacity_reservations)
            >= self._MAX_CONCURRENT_EXECUTIONS
        ):
            return False
        if pending.id in self._inflight_executions:
            return True
        registration = self._runtime_work_tokens.get(RuntimeWorkLane.REQUESTS)
        self._request_capacity_reservations.add(pending.id)
        try:
            if not self._transport_ready_for_request(pending):
                await self._run_runtime_sync(
                    self.request_store.record_skip_reason,
                    pending.id,
                    reason=SKIP_REASON_TRANSPORT_UNAVAILABLE,
                )
                return False
            lock_key = self._execution_lock_key(pending)
            if lock_key is not None and lock_key in self._inflight_sessions:
                await self._run_runtime_sync(
                    self.request_store.record_skip_reason,
                    pending.id,
                    reason=SKIP_REASON_SESSION_BUSY,
                )
                return False
            command_definition_id = self._command_fire_definition_id(pending)
            if command_definition_id is not None and await self._run_runtime_sync(
                self._command_definition_has_live_worker,
                command_definition_id,
            ):
                await self._run_runtime_sync(
                    self.request_store.record_skip_reason,
                    pending.id,
                    reason=SKIP_REASON_SESSION_BUSY,
                )
                return False
            request = await self._run_runtime_sync(self._claim_pending_request, pending)
            if request is None:
                return True
            if registration is not None and (
                not self._running
                or self._runtime_work_tokens.get(RuntimeWorkLane.REQUESTS)
                != registration
                or not self._owns_service_instance()
            ):
                await self._run_runtime_sync(
                    self.request_store.requeue,
                    request.id,
                )
                return True
            self._spawn_execution(request, lock_key)
            return True
        finally:
            self._request_capacity_reservations.discard(pending.id)

    async def _process_run_callback(self, run: dict[str, Any]) -> bool:
        run_id = str(run.get("id") or "")
        if not run_id:
            return True
        try:
            callback_run = await self._run_runtime_sync(
                self._enqueue_callback_run,
                run,
            )
        except Exception as exc:
            logger.error("Agent run callback failed for %s: %s", run_id, exc, exc_info=True)
            await self._run_runtime_sync(
                self.request_store.update_callback_status,
                run_id,
                status="failed",
                error=str(exc),
            )
            return False
        if callback_run is None:
            await self._run_runtime_sync(
                self.request_store.update_callback_status,
                run_id,
                status="skipped",
            )
            return True
        await self._run_runtime_sync(
            self.request_store.update_callback_status,
            run_id,
            status="sent",
            callback_run_id=callback_run.id,
        )
        self._wake_runtime_work(RuntimeWorkLane.REQUESTS)
        return True

    async def _drain_requests(self) -> None:
        if not self._owns_service_instance():
            return
        # Claim eligible pending requests and dispatch each as its own task,
        # then return immediately. The previous implementation awaited every
        # execution inline in this loop, so one turn that hung (e.g. an agent
        # backend that never returns) blocked the loop forever and every
        # later request piled up in ``queued``. Dispatching concurrently keeps
        # delivery flowing: a stuck turn only holds up its own session.
        for pending in self.request_store.list_pending():
            if len(self._inflight_executions) >= self._MAX_CONCURRENT_EXECUTIONS:
                # At capacity — leave the rest queued and retry next tick.
                # Crucially we never await here, so the loop can't be stalled.
                break
            if pending.id in self._inflight_executions:
                continue
            if not self._transport_ready_for_request(pending):
                # Record it: this is the only skip reason that eventually makes the row
                # sweepable, and the sweep reads the reason rather than re-deriving
                # readiness. Transition-only inside the store, so a transport that
                # stays down does not turn this into a per-tick write.
                self.request_store.record_skip_reason(
                    pending.id, reason=SKIP_REASON_TRANSPORT_UNAVAILABLE
                )
                continue
            lock_key = self._execution_lock_key(pending)
            if lock_key is not None and lock_key in self._inflight_sessions:
                # A turn for this conversation is already running; keep this
                # one queued so we never run two turns for one session at once.
                # The next drain tick picks it up once the session frees.
                # Recorded so it can clear a stale transport reason — this row is
                # making progress and must not look sweepable.
                self.request_store.record_skip_reason(pending.id, reason=SKIP_REASON_SESSION_BUSY)
                continue
            command_definition_id = self._command_fire_definition_id(pending)
            if command_definition_id is not None and self._command_definition_has_live_worker(
                command_definition_id
            ):
                # Same reason as the lock above and recorded the same way: the definition
                # is busy -- here with a worker this process did not start -- so the row
                # keeps waiting and must not look sweepable while it does.
                self.request_store.record_skip_reason(pending.id, reason=SKIP_REASON_SESSION_BUSY)
                continue
            request = self._claim_pending_request(pending)
            if request is None:
                continue
            self._spawn_execution(request, lock_key)

    async def _drain_callbacks(self) -> None:
        if not self._owns_service_instance():
            return
        for run in self.request_store.list_pending_callbacks():
            run_id = str(run.get("id") or "")
            if not run_id:
                continue
            try:
                callback_run = self._enqueue_callback_run(run)
            except Exception as exc:
                logger.error("Agent run callback failed for %s: %s", run_id, exc, exc_info=True)
                self.request_store.update_callback_status(run_id, status="failed", error=str(exc))
                self._wake_runtime_work(RuntimeWorkLane.RUN_CALLBACKS)
                continue
            if callback_run is None:
                self.request_store.update_callback_status(run_id, status="skipped")
                self._wake_runtime_work(RuntimeWorkLane.RUN_CALLBACKS)
                continue
            self.request_store.update_callback_status(run_id, status="sent", callback_run_id=callback_run.id)
            self._wake_runtime_work(
                RuntimeWorkLane.REQUESTS,
                RuntimeWorkLane.RUN_CALLBACKS,
            )

    # --- the owed-failure-notice drain ---------------------------------------
    #
    # Rides the existing 2 s tick beside ``_drain_callbacks``, for the same reason:
    # a failed run emits no store change anyone else notices, so only a periodic
    # pass finds it. The durable notice is stamped by whichever UPDATE terminalizes
    # the row, so a crash between the failure and the delivery loses nothing — which
    # is the whole point of persisting it rather than notifying inline.

    def _spawn_failure_notice_drain(self) -> None:
        """Start the owed-notice drain OUTSIDE the store watch, one pass at a time.

        ``_watch_store`` is a single coroutine running every periodic pass in sequence,
        so awaiting the drain there puts external message delivery on the critical path
        of the whole service tick. One notice whose delivery does not return stops
        request draining, callbacks, vault callbacks, the stale-run sweep — and every
        LATER notice, including the ones reporting the failures that wedge is a symptom
        of. Nothing looks broken, because the loop is suspended rather than crashed.

        ``NOTICE_DELIVERY_TIMEOUT_SECONDS`` alone would only shorten that stall to the
        deadline, and a deadline short enough to hold the tick would cancel legitimate
        deliveries. So the two halves are both required: dispatch takes delivery off the
        watch, and the deadline bounds the dispatched work.

        SINGLE-FLIGHT is the other half of dispatching. A task per tick would replace a
        stalled loop with a delivery attempt every 2 s — the durable claim would make
        most of them stand down, but the pile of coroutines is unbounded and each one
        re-reads and re-claims. One pass at a time, and a tick that finds the previous
        pass still running simply skips: the drain is idempotent across ticks by
        construction, since eligibility is re-read from the row every pass.
        """

        task = self._notice_drain_task
        if task is not None and not task.done():
            return
        self._notice_drain_task = asyncio.create_task(self._drain_failure_notices())
        self._notice_drain_task.add_done_callback(self._on_notice_drain_done)

    @staticmethod
    def _on_notice_drain_done(task: "asyncio.Task[Any]") -> None:
        """Retrieve the dispatched pass's exception so it is logged, never swallowed.

        The inline await propagated failures into the watch's own handler. A fire-and-
        forget task has no such reader, and an unretrieved exception surfaces only as an
        asyncio "never retrieved" warning at garbage-collection time — which is how a
        drain that has been failing every tick for a week goes unnoticed.
        """

        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("owed failure notice drain pass failed: %r", exc, exc_info=exc)

    async def _drain_failure_notices(self) -> None:
        if not self._owns_service_instance():
            return
        store = self.request_store._sqlite
        if store is None:
            return
        try:
            owed = store.list_owed_failure_notices(limit=10)
        except Exception:
            logger.exception("failed to list owed failure notices")
            return
        # THE FAIRNESS BUDGET, checked between notices and never against one in flight.
        #
        # Deliveries inside a pass are serial on purpose (see
        # ``NOTICE_DRAIN_PASS_BUDGET_SECONDS``), so a batch of wedged rows costs
        # batch x deadline before the pass returns and every notice stamped in the
        # meantime waits behind it. Truncating the pass bounds that wait by the budget
        # plus one delivery instead of by the batch size.
        #
        # Between, not during: cancelling a delivery already on the wire would trade a
        # slow pass for a duplicate, and the walk already has its own deadline. So the
        # pass always finishes what it started and only stops PULLING more.
        #
        # Rows left behind cost nothing — unclaimed, so no attempt consumed and no
        # backoff armed — and ``list_owed_failure_notices`` orders by
        # ``next_attempt_at ASC``, so the next pass starts with the oldest of them.
        started = time.monotonic()
        budget = failure_notices.NOTICE_DRAIN_PASS_BUDGET_SECONDS
        for index, run in enumerate(owed):
            if index and time.monotonic() - started > budget:
                logger.info(
                    "owed failure notice pass stopped at its %ss budget with %d of %d "
                    "notices unattempted; they keep their attempts and stay eligible",
                    budget,
                    len(owed) - index,
                    len(owed),
                )
                break
            run_id = str(run.get("id") or "")
            if not run_id:
                continue
            try:
                await self._deliver_one_failure_notice(store, run)
            except Exception:
                logger.exception("owed failure notice drain failed for run=%s", run_id)

    async def _deliver_one_failure_notice(self, store: Any, run: dict[str, Any]) -> None:
        run_id = str(run["id"])
        definition_id = str(run.get("task_id") or run.get("definition_id") or "") or None
        notice = await self._run_runtime_sync(store.owed_failure_notice, run_id)
        # Re-read and re-check ELIGIBILITY, not merely the state. The batch was listed
        # before this pass began and each row is then delivered one at a time, so by
        # the time a row is reached another owner may already have claimed it — and a
        # claim is expressed as ``pending`` plus a lease on ``next_attempt_at``, so a
        # state-only check would read it as free and send a duplicate. This is the same
        # predicate the listing query used, deliberately: whatever the claim writes has
        # to be visible to every later reader through one shared definition of
        # eligibility.
        now = datetime.now(timezone.utc)
        if not owed_notice_eligible(notice, now.isoformat()):
            return
        # Every write below re-asserts the notice this pass DECIDED FROM. Ownership is
        # checked once, at the top of the pass, and then the pass awaits delivery — so a
        # service-lock handoff can leave this coroutine writing behind the new owner's
        # completed delivery. The expectation makes the loser a no-op instead.
        expect = notice_write_expectation(notice)

        streak_facts: Optional[dict[str, Any]] = None
        earlier_unsettled = None
        # ``bypasses_suppression``, not ``is_interruption``: a binding-change notice is
        # already scoped by the transition's signature, and reading the streak for one
        # would be actively wrong — the anchor run SUCCEEDED, so
        # ``failure_streak_decision`` would sweep in the definition's surrounding
        # failures and defer the notice behind a canonical row it has nothing to do
        # with.
        if definition_id and not failure_notices.bypasses_suppression(notice):
            earlier_unsettled = await self._run_runtime_sync(
                store.earliest_unsettled_run_before,
                definition_id,
                created_at=str(run.get("created_at") or ""),
                run_id=run_id,
                stale_after_seconds=failure_notices.DEFERRAL_STALE_AFTER_SECONDS,
            )
            if earlier_unsettled is None:
                # The DECISION facts, not the streak's rows. One statement, so the
                # boundaries and the notice states inside them come from one SQLite
                # read snapshot: read separately, a success settling between the
                # boundary seek and the row read merges two streaks, and a ``sent``
                # notice from the earlier outage then skips a live one.
                streak_facts = await self._run_runtime_sync(
                    store.failure_streak_decision,
                    definition_id,
                    run_id,
                )

        # The callback's status is read FRESH, not taken from the listed row: the
        # batch predates this decision by up to a whole pass, and ``_drain_callbacks``
        # runs on the same ticks — a stale ``pending`` here would defer a notice whose
        # callback already landed, and a stale absence would deliver beside it. A
        # read failure propagates to the drain loop's per-row handler and the row is
        # retried later, which errs toward one message rather than two.
        turn_id = failure_notices.notice_turn_id(notice)
        if turn_id and hasattr(store, "turn_callback_state"):
            participant_run_ids = notice.get("turn_participant_run_ids")
            callback_status = await self._run_runtime_sync(
                store.turn_callback_state,
                turn_id,
                participant_run_ids=(
                    participant_run_ids
                    if isinstance(participant_run_ids, list)
                    else []
                ),
            )
        else:
            callback_status = await self._run_runtime_sync(
                store.run_callback_state,
                run_id,
            )
        decision = failure_notices.decide(
            run_id=run_id,
            definition_id=definition_id,
            notice=notice,
            streak_facts=streak_facts,
            earlier_unsettled=earlier_unsettled,
            callback_status=callback_status,
        )
        if decision.action == failure_notices.ACTION_DEFER:
            # No attempt consumed — this row has not been tried. But the deferral is
            # written down rather than merely skipped: a row left immediately-eligible
            # is re-selected by every tick and keeps occupying the batch, so one
            # definition with more than a batch worth of pending failures starved
            # every other definition's notices indefinitely.
            await self._run_runtime_sync(
                store.update_owed_failure_notice,
                run_id,
                expect=expect,
                next_attempt_at=(
                    datetime.now(timezone.utc)
                    + timedelta(seconds=failure_notices.DEFERRAL_RECHECK_SECONDS)
                ).isoformat(),
                defer_reason=decision.reason,
            )
            logger.debug("failure notice for %s deferred (%s)", run_id, decision.reason)
            return
        if decision.action == failure_notices.ACTION_SKIP:
            await self._run_runtime_sync(
                store.update_owed_failure_notice,
                run_id,
                expect=expect,
                state=NOTICE_SKIPPED,
                skip_reason=decision.reason,
            )
            return

        attempt, retry_after = failure_notices.next_attempt(notice)

        # THE CLAIM, and it must precede the external side effect.
        #
        # This one guarded UPDATE does two things at once: it CONSUMES the attempt (so
        # the number is durable before anything can go wrong with the send — the bound
        # the raising-rung handler below exists to keep) and it arms a LEASE on
        # ``next_attempt_at`` marking the row as somebody's until that instant.
        #
        # Why here and not after the send. Ownership is checked once per pass and then
        # the pass AWAITS delivery, so two owners can both hold a listed row: they both
        # read ``pending``, both walk the ladder, both send, and only then does either
        # write. A predicate on the write catches the second WRITE and nothing else —
        # the user already has two messages, and no database can recall one. Nothing
        # downstream closes it either: ``emit_agent_message`` checks
        # ``agent_message_exists`` BEFORE the send and persists AFTER it, so both owners
        # pass that lookup while neither receipt exists. Single-flight has to be
        # established before the irreversible act, which means before this line.
        #
        # The primitive is the CAS that is already here. SQLite evaluates ``expect`` in
        # the writing statement under its single-writer lock, so a guarded transition
        # from ``(pending, N)`` is atomic across connections and processes — exactly an
        # atomic claim, just used earlier. An owner that read before this write loses
        # the CAS; one that reads after it sees the lease and stands down at the
        # eligibility check at the top of this method.
        #
        # And because the lease is an INSTANT rather than a held lock, a claimant that
        # dies mid-send releases it by expiry: ``CLAIM_LEASE_SECONDS`` is the recovery
        # bound, and the recovered pass consumes its own attempt rather than inheriting
        # the dead one, so the retry ladder stays finite. The residual is at-least-once
        # delivery, documented on that constant.
        claimed = await self._run_runtime_sync(
            store.update_owed_failure_notice,
            run_id,
            expect=expect,
            attempts=attempt,
            # Armed at CLAIM time, not from the instant the eligibility check read:
            # the streak reads above sit between the two, and the lease has to bound
            # the delivery that is about to start rather than one that already has.
            next_attempt_at=(
                datetime.now(timezone.utc)
                + timedelta(seconds=failure_notices.CLAIM_LEASE_SECONDS)
            ).isoformat(),
        )
        if claimed is None:
            # Another owner moved this row between our read and our claim. Nothing to
            # repair and nothing to report: the winner is delivering it, and this pass
            # must not send. Silent for the same reason the losing write is — a lock
            # handoff is normal, and an error here would be logged on every one.
            logger.debug("failure notice for %s already claimed by another owner", run_id)
            return
        # Every write below re-asserts what the CLAIM left behind, not what was read
        # before it.
        expect = notice_write_expectation(claimed)

        evidence = DeliveryEvidence()
        # THE DEADLINE, over the whole ladder walk rather than one rung.
        #
        # The claim above makes a competing owner stand down for the lease, and lease
        # expiry recovers a claimant that DIES. Neither covers one that never returns:
        # a transport that accepted the request and hung leaves this coroutine
        # suspended with the row ineligible and nothing reporting it, so the notice is
        # owed indefinitely. See ``NOTICE_DELIVERY_TIMEOUT_SECONDS`` for the two-sided
        # argument for the value; what matters here is the scope and the disposal.
        #
        # Scope is the WALK, not the rung: a per-rung bound would let five slow rungs
        # add up past the lease, and the thing being bounded is how long this claim can
        # be held.
        #
        # Disposal: ``wait_for`` cancels the inner task and AWAITS that cancellation
        # before raising, so the transport coroutine is dead — not detached to send
        # behind a replacement claimant's back — by the time the handler below writes
        # anything. ``asyncio.wait_for`` rather than ``asyncio.timeout`` because the
        # package supports 3.10, where the latter does not exist.
        # The ATTEMPT IS NOT THREADED INTO THE WALK, and it used to be. An earlier
        # revision passed it down because the workspace rung's presence depended on the
        # attempt number; that design is retired (see ``_failure_notice_targets``' WHEN
        # block) and the ladder is now the same on every attempt, so the walk needs
        # nothing from the counter. ``attempt`` still governs everything BELOW — the
        # backoff instant, ``MAX_ATTEMPTS``, and the dead letter — because those are
        # properties of the row, not of the address.
        emit = asyncio.ensure_future(self._emit_failure_notice(run, notice, evidence))
        try:
            delivered = await asyncio.wait_for(
                emit, timeout=failure_notices.NOTICE_DELIVERY_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError as exc:
            # WHOSE timeout, asked rather than assumed: from 3.11 ``asyncio.TimeoutError``
            # is the builtin ``TimeoutError``, so an adapter's own HTTP timeout arrives
            # here indistinguishable from the deadline. ``emit.cancelled()`` is the
            # discriminator — only the deadline cancels the walk — and without it the
            # notice would be stamped with a confident lie about which one happened.
            delivered = False
            if emit.cancelled():
                logger.error(
                    "failure notice delivery timed out for run=%s after %ss; transport cancelled",
                    run_id,
                    failure_notices.NOTICE_DELIVERY_TIMEOUT_SECONDS,
                )
                # The timeout CONSUMES the attempt the claim already made durable: the
                # retry write below arms the ordinary backoff under the claim's own
                # expectation, which cannot lose while this claimant's lease holds. A
                # release (rewinding ``attempts``) would let a permanently hanging
                # transport retry without bound and never dead-letter.
                stamped: BaseException = TimeoutError(
                    "failure notice delivery timed out after "
                    f"{failure_notices.NOTICE_DELIVERY_TIMEOUT_SECONDS}s; transport cancelled"
                )
            else:
                # A rung's OWN timeout, reported as itself: it is an ordinary raising
                # rung that happens to have picked this exception type.
                logger.exception("failure notice delivery raised for run=%s", run_id)
                stamped = exc
            if evidence.error is None:
                evidence.error = stamped
                evidence.error_stage = "deliver"
        except Exception as exc:
            # A raising rung CONSUMES an attempt. Previously the exception escaped
            # between computing the attempt number and persisting it, so the next
            # 2 s tick recomputed the same number and raised again — an unbounded
            # retry loop, which is exactly what the backoff exists to prevent. The
            # bound has to hold for any rung, not just the one that was observed
            # raising, so this catches rather than enumerating call sites.
            logger.exception("failure notice delivery raised for run=%s", run_id)
            delivered = False
            if evidence.error is None:
                evidence.error = exc
                evidence.error_stage = "deliver"

        if delivered and evidence.delivered:
            # Acknowledged on a durable receipt or on a returned delivery id — never
            # on a function return. ``emit_replayed_backend_failure`` discards the
            # notify result and returns normally either way, so a returns-cleanly ack
            # would flip a lost notice to ``sent`` permanently.
            await self._run_runtime_sync(
                store.update_owed_failure_notice,
                run_id,
                expect=expect,
                state=NOTICE_SENT,
                # Re-asserting the attempt the CLAIM already consumed, deliberately
                # rather than omitting it: this write is the one a reader reconstructs
                # the delivery from, and it should say which attempt succeeded instead
                # of leaving that to be inferred from an earlier row version.
                attempts=attempt,
                ack_evidence=evidence.ack_evidence,
                # A post-delivery error (the SSE fan-out raised) is recorded for
                # diagnosis and must NOT trigger a resend.
                error=evidence.error_text,
            )
            self._wake_runtime_work(RuntimeWorkLane.FAILURE_NOTICES)
            return

        error_text = evidence.error_text or "failure notice delivery produced no evidence"
        if retry_after is None:
            # Dead letter, carrying the raised exception's own message rather than a
            # generic string. Visible rather than silently retrying forever.
            await self._run_runtime_sync(
                store.update_owed_failure_notice,
                run_id,
                expect=expect,
                state=NOTICE_FAILED,
                attempts=attempt,
                error=error_text,
            )
            logger.error("failure notice for run %s dead-lettered: %s", run_id, error_text)
            return
        next_attempt_at = (
            datetime.now(timezone.utc) + timedelta(seconds=retry_after)
        ).isoformat()
        await self._run_runtime_sync(
            store.update_owed_failure_notice,
            run_id,
            expect=expect,
            state=NOTICE_PENDING,
            attempts=attempt,
            next_attempt_at=next_attempt_at,
            error=error_text,
        )

    async def _emit_failure_notice(
        self,
        run: dict[str, Any],
        notice: dict[str, Any],
        evidence: "DeliveryEvidence",
    ) -> bool:
        """Walk D5's delivery ladder for one failed run. True once one rung emitted.

        Evidence is per RUNG, then adopted into the caller's object. One shared
        ``DeliveryEvidence`` cannot express a ladder: ``delivered`` latches true the
        moment any rung records an id, so a rung whose ack is REJECTED (see
        ``_rung_acknowledges``) would both stop the walk and hand the eventual
        ack/dead-letter another rung's ``ack_evidence``. The caller ends up with the
        winning rung's evidence, or — when no rung was accepted — the last one's, so
        the dead letter reports what actually went wrong on the final attempt.

        A rung that RAISES is an unusable rung, not the end of the walk. Every other
        way a rung can fail to deliver already continues — ``_build_context`` raising,
        a stale synthetic candidate, an un-acked send — so a delivery that throws
        (a platform whose settings manager is gone, an adapter that fails before its
        transport) has to continue too, or the notice spends every attempt on rung (1)
        and dead-letters without rungs (2)…(5) ever being tried. ``Exception`` and not
        ``BaseException``: the walk-level deadline cancels this coroutine, and
        cancellation must unwind the walk rather than advance it past the bound it was
        cancelled to respect.

        The cost is real and is recorded rather than hidden. A rung that raises AFTER
        its transport accepted the send, leaving no evidence behind, now delivers again
        on the next rung. The adapters no longer manufacture that state — post-send
        bookkeeping is guarded in every one of them, so an already-delivered id is not
        destroyed on its way out — and what remains is the same at-least-once residual
        documented on ``CLAIM_LEASE_SECONDS``, narrowed on the retry by the duplicate
        short-circuit's persisted receipt.

        THE WORKSPACE RUNG IS RESOLVED HERE, NOT WHERE THE LADDER IS BUILT, and that
        split is the whole reason this method knows the reserved id at all.
        ``_failure_notice_targets`` appends the reserved id as a CONSTANT — no database
        access, no row minted — and the resolve-or-create-or-heal happens below, once
        the walk has actually reached that rung. Two consequences, both wanted:

        * an installation whose rung (1) always delivers never grows the reserved row,
          even though every ladder it builds ends with the reserved id. The rung is
          appended to every ladder unconditionally (the round-14 gate), so a
          build-time resolve would create the row on the FIRST failure of every
          install, which is the one surviving argument from the design this replaced;
        * ``_failure_notice_targets`` stays a pure address computation. It is called
          directly by tests and by ad-hoc inspection, and under a build-time resolve
          each of those calls would be a write.

        The cost is that the rung can turn out to be UNUSABLE mid-walk, when the
        workbench database cannot be read or written. That is not a new state — every
        other rung can be unusable too (``_build_context`` raising, a stale candidate,
        a send that throws) — and it takes the same disposal: the rung is skipped, the
        walk continues, and with nothing left to try the notice keeps its ``pending``
        state and its backoff. It dead-letters visibly only if the database never
        answers.
        """

        body = self._failure_notice_body(run, notice)
        failure_id = str(notice.get("failure_id") or f"failure:{run['id']}")
        last_rung: Optional[DeliveryEvidence] = None
        first_raise: Optional[DeliveryEvidence] = None
        try:
            for target, session_id in self._failure_notice_targets(run):
                if session_id == WORKSPACE_NOTICE_SESSION_ID:
                    # THE LAZY HALF of the reserved rung: resolve-or-create-or-heal now
                    # that the walk has reached it. ``resolve_workspace_notice_session``
                    # always answers with this same reserved id, so nothing about the
                    # target changes — the call is made for its WRITE (create the row,
                    # or repair one that was archived or hidden), not for its return.
                    if self._workspace_notice_session_id() is None:
                        # An unusable rung, disposed of like any other. Recorded only
                        # when no earlier rung left evidence: a preferred rung's own
                        # refusal ("returned send id … without a persisted receipt") is
                        # the more useful diagnosis, and this must not displace it.
                        logger.warning(
                            "failure notice rung unusable: the workspace-notifications "
                            "session could not be resolved for run=%s",
                            run["id"],
                        )
                        if last_rung is None:
                            unusable = DeliveryEvidence()
                            unusable.error = RuntimeError(
                                f"{target.to_key()} rung unusable: the "
                                "workspace-notifications session could not be resolved"
                            )
                            unusable.error_stage = "deliver"
                            last_rung = unusable
                        continue
                try:
                    context = await self._build_context(
                        target,
                        delivery_target=target,
                        execution_id=str(run["id"]),
                        task_id=str(run.get("task_id") or "") or None,
                        trigger_kind=str(run.get("run_type") or "scheduled"),
                        session_id=session_id,
                    )
                except Exception:
                    logger.debug("failure notice rung unusable: %s", target, exc_info=True)
                    continue
                rung = DeliveryEvidence()
                last_rung = rung
                # The REPLAY emitter, not the live failure path. A notice is owed only for
                # a run that is already settled, so this delivers one visible ``notify``
                # and nothing else: no terminal result, no turn settlement, no auth
                # prompt, and an identity taken from the durable row rather than from
                # whatever ``task_execution_id`` this rebuilt context happens to supply.
                # See ``emit_replayed_backend_failure`` for why each of those is a
                # property of the emitter instead of an argument to the live one.
                try:
                    await emit_replayed_backend_failure(
                        self.controller,
                        context,
                        str(run.get("agent_backend") or "harness"),
                        str(run.get("error") or "").strip() or body,
                        display_text=body,
                        failure_id=failure_id,
                        delivery=rung,
                    )
                except Exception as exc:
                    # This rung is unusable; the ladder is not. See the docstring for
                    # why ``Exception`` and not ``BaseException``.
                    logger.warning(
                        "failure notice rung raised, continuing the walk: %s",
                        target.to_key(),
                        exc_info=True,
                    )
                    if rung.error is None:
                        rung.error = exc
                        rung.error_stage = "deliver"
                    if first_raise is None:
                        first_raise = rung
                    continue
                if self._rung_acknowledges(target, rung):
                    return True
        finally:
            if last_rung is not None:
                _adopt_delivery_evidence(evidence, last_rung)
            if evidence.error is None and first_raise is not None:
                # The winning rung carries no error of its own, so the skipped rung's
                # does not compete with anything: it is recorded on the acknowledged
                # row purely for diagnosis, the same way a post-delivery stream error
                # is, and — like that one — must never be read as a delivery failure.
                evidence.error = first_raise.error
                evidence.error_stage = first_raise.error_stage
        return False

    @staticmethod
    def _rung_acknowledges(target: ParsedSessionKey, rung: "DeliveryEvidence") -> bool:
        """Whether THIS rung's evidence satisfies its target class's ack source.

        One table lookup and one membership test, deliberately: the question "who may
        ack on what" is answered once, declaratively, by ``LADDER_ACK_SOURCES`` — see
        that table for why each class gets the source it does. A predicate that
        special-cased platforms here is exactly how three review rounds each found a
        different target class acking on evidence that proved nothing.

        A rejected rung is not a silent one. When the send DID return an id and only
        the durable receipt is missing, that is recorded on the rung so the eventual
        retry or dead letter can say why, instead of reporting "produced no
        evidence" about a send that in fact returned.
        """

        source = failure_notice_ack_source(target)
        if rung.ack_evidence in ACK_EVIDENCE_BY_ACK_SOURCE[source]:
            return True
        if (
            source == ACK_SOURCE_PERSISTED_RECEIPT
            and rung.error is None
            and rung.delivered_id is not None
        ):
            rung.error = RuntimeError(
                f"{target.to_key()} rung returned send id {rung.delivered_id} "
                "without a persisted receipt"
            )
            rung.error_stage = STAGE_PERSIST
        return False

    def _failure_notice_targets(
        self,
        run: dict[str, Any],
    ) -> list[tuple[ParsedSessionKey, Optional[str]]]:
        """D5's ladder, in order, skipping rungs this run cannot address.

        (1) the definition's delivery key; (2) the bound session's scope while the
        session is still alive; (3) the scope the definition was created from;
        (4) a DM to the owner; (5) the workbench inbox.

        Rung (5) carries the definitions no person is addressable for. For one
        created by a plain ``vibe task add`` at a terminal there is no caller
        provenance at all, so rungs (3) and (4) are both empty; an unscoped
        ``create_per_run`` definition can also have no delivery key, and once its
        per-run session is gone rung (2) goes with it.

        What rung (5) does and does not guarantee, stated honestly because the
        earlier "always resolves" claim was wrong. The distinction it turns on is
        between a REAL PERSISTED project scope and a STALE SYNTHETIC project
        candidate, and the key alone does not tell them apart: rung (5) is spelled
        ``avibe::project::<session id>``, a candidate this method MANUFACTURES for
        every run carrying a session id. It becomes a real scope only downstream,
        where ``persist_agent_message`` looks the session's ``agent_sessions`` row up
        and takes that row's ``scope_id``.

        AN ARCHIVED OR BACKGROUND ROW IS NOT A DELIVERY SURFACE, and rung (5) is
        rerouted when the run names one — see ``_rung_five_session_id``. The previous
        revision claimed
        the opposite ("the real difference between this rung and rung (2)": that
        ``_session_row`` has no status filter while ``resolve_session_id_target``
        refuses an archived session outright). The mechanism is real; the conclusion
        was wrong, and it is the round-13 P1 on this method (review thread
        3676292667). ``persist_agent_message`` does write the row, which is the
        workbench class's ack source, so the notice is stamped ``sent`` — while
        ``list_inbox_sessions`` excludes archived and background sessions, so there
        is no card, no
        ``inbox.session.updated`` and no push, and the acked notice is never retried.
        Same class as the round-12 reserved-session hole, opposite remedy: the
        reserved row is HEALED because nobody may archive it, whereas an ordinary
        session was archived by its owner ON PURPOSE. Resurrecting it would overrule
        the user, so the fix is ROUTING.

        With a hard-deleted row the candidate resolves to nothing and
        ``persist_agent_message`` returns before writing — yet ``AvibeBot.send_message``
        still hands back a synthetic ``msg_…`` id, so the rung LOOKS delivered. That is
        why the workbench target class may only acknowledge on a persisted receipt (see
        ``LADDER_ACK_SOURCES``): a stale candidate leaves the notice retryable instead
        of marking it ``sent`` against a row that was never written. Pinned by
        ``test_an_avibe_rung_does_not_ack_on_a_synthetic_send_id``.

        AND EVERY LADDER ENDS WITH THE RESERVED WORKSPACE RUNG (plan :3193, :3215-3222;
        round-14 gate, review comment 5121007240). One distinct
        WORKSPACE-NOTIFICATIONS rung is appended after every person/context target, on
        every attempt, whatever the four rungs above produced — see the WHEN block at
        the end of this method for why that is unconditional and what it costs. It
        resolves without a person to address because it is addressed to the workspace
        instead.

        This method takes NO attempt number and performs NO database access. The
        reserved rung is appended as a CONSTANT (``WORKSPACE_NOTICE_SESSION_ID``); the
        resolve-or-create-or-heal happens in ``_emit_failure_notice``, when the walk
        actually reaches that rung. So the ladder is a pure address computation that is
        safe to call from a test or an ad-hoc probe, and an installation whose rung (1)
        always delivers never grows the reserved row even though every ladder it builds
        names it.

        The rung is therefore appended UNCONDITIONALLY but is not guaranteed USABLE, and
        that distinction is stated here rather than left to be discovered because an
        earlier revision's unqualified "always resolves" is the exact claim the plan's
        own correction had to retract. ``_workspace_notice_session_id`` returns ``None``
        when the workbench database cannot be read or written; the walk then skips the
        rung. The consequence is a RETRY, not a loss: the notice keeps its ``pending``
        state, arms its backoff, and delivers on a later pass once the database answers
        — and only dead-letters if it never does. That is the same shape as any other
        unusable rung, which is why it needs no special handling in the drain.
        """

        rungs: list[tuple[ParsedSessionKey, Optional[str]]] = []
        seen: set[tuple[str, Optional[str]]] = set()

        def _add(raw_key: Any, session_id: Optional[str]) -> None:
            key = str(raw_key or "").strip()
            if not key:
                return
            try:
                parsed = parse_session_key(key)
            except Exception:
                # ``parse_session_key`` rejects every scope type outside
                # ``{channel, user}``, and EVERY avibe rung is
                # ``avibe::project::…`` — rung (2) for any workbench-bound session
                # (``resolve_session_id_target`` hands back a ``project`` scope for
                # one), rung (3) for a workbench-created definition, and rung (5)
                # always. Swallowing that ``ValueError`` silently discarded all of
                # them, so an Avibe-only definition had an entirely empty ladder.
                #
                # Only a bare three-part key falls back. A five-part
                # ``::thread::`` key is a session key by construction and
                # ``parse_scope_id`` cannot express one, so it stays on the strict
                # parser rather than being downgraded to its scope prefix (which
                # would silently retarget a thread notice at its parent channel).
                if len(key.split("::")) != 3:
                    return
                try:
                    parsed = parse_scope_id(key)
                except Exception:
                    return
            identity = (parsed.to_key(), session_id)
            if identity in seen:
                return
            seen.add(identity)
            rungs.append((parsed, session_id))

        metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
        task = self.store.get_task(str(run.get("task_id") or "")) if run.get("task_id") else None

        # (1) the definition's delivery key.
        _add((task.deliver_key if task else None) or run.get("deliver_key"), None)

        # (2) the bound session's scope, only while the session resolves to a visible
        # delivery surface. Background sessions deliberately suppress delivery, so
        # persisting and acknowledging a notice there would hide it permanently.
        session_id = str((task.session_id if task else None) or run.get("session_id") or "").strip()
        if session_id:
            try:
                resolved = resolve_session_id_target(session_id)
            except Exception:
                resolved = None
            if resolved is not None and not resolved.suppress_delivery:
                _add(resolved.session_key.to_key(), session_id)

        # (3) caller provenance, written at definition creation.
        caller = _created_by_caller(task, metadata)
        if caller is not None:
            _add(caller.get("session_key") or caller.get("scope_id"), None)

        # (4) a DM to the owner, from the same provenance.
        if caller is not None:
            platform = str(caller.get("platform") or "").strip()
            user_id = str(caller.get("user_id") or "").strip()
            if platform and user_id:
                _add(f"{platform}::user::{user_id}", None)

        # (5) the workbench inbox, addressed through the run's own session — the rung
        # that survives rung (2)'s refusal, because ``persist_agent_message`` resolves
        # the avibe scope from ``_session_row`` (no status filter) where
        # ``resolve_session_id_target`` demands a live, usable session.
        #
        # The key below is a CANDIDATE, not a resolved scope: the session id sits in
        # the ``project`` slot and, for a MISSING row, nothing here checks that the row
        # it names still exists. Whether it is real is settled downstream, by whether a
        # durable ``messages`` row appears — which is exactly what the workbench class's
        # receipt-only ack source measures, so a candidate for a deleted session
        # cannot pass itself off as a delivery.
        #
        # An archived or background row is the case that measurement CANNOT catch,
        # so it is caught here instead: see ``_rung_five_session_id``.
        #
        # So this rung covers every definition that has ever had a session — every
        # ``create_once`` / ``create_per_run`` / session-bound definition — addressing
        # the run's own session while that row is a surface a user can see, and the
        # workspace inbox when it is not.
        if session_id:
            rung_five_session_id = self._rung_five_session_id(session_id)
            _add(f"avibe::project::{rung_five_session_id}", rung_five_session_id)

        # …AND THE LAST RUNG, for the definitions the four above cannot address at all
        # (plan :3193, :3215-3222; PR6's own step list, :1256-1259).
        #
        # THE EARLIER POSITION HERE WAS WRONG, and the plan says so with a date. It read
        # that ``persist_agent_message`` returns before writing when an avibe context
        # resolves neither a scope nor a session row, therefore a session-less
        # definition has nowhere to put the row, therefore the notice dead-letters
        # VISIBLY and that is a declared Known-By-Design limitation under #1044. The
        # first two clauses are still true; the conclusion was not. "Visible in
        # ``last_error`` and the health badge" is visible to somebody who goes LOOKING,
        # and D1's whole subject is the runs nobody is watching. A notice with nowhere
        # to go is a notice that is never written.
        #
        # What was actually missing was a HOME, not a widened writer: the blocker is a
        # session row, so the fix supplies one. ``_workspace_notice_session_id``
        # resolves-or-creates a single reserved workspace-notifications session, and
        # rung (5) then addresses it exactly like any other avibe session — the same
        # ``avibe::project::<session id>`` candidate, satisfied by the same
        # ``_session_row`` lookup, acked by the same receipt-only policy. Nothing
        # downstream learns a new row shape.
        #
        # WHAT THAT ACTUALLY GETS THE USER, enumerated rather than waved at, because the
        # previous revision's "the same inbox / unread / realtime / Web Push machinery"
        # claimed three surfaces the row does not reach:
        #
        # * an INBOX CARD (``list_inbox_sessions`` accepts a terminal ``notify``) and the
        #   ``inbox.session.updated`` realtime event that patches an open browser — the
        #   two surfaces the plan's "readable inbox row" is about;
        # * a LOCAL Web Push, via ``maybe_notify_inbox_message``.
        #
        # Residuals, all three properties of pre-existing policy rather than of this rung:
        # the notice does NOT bump the unread badge (``notify`` carries no ``unread`` in
        # ``vibe/message_types.json`` and the unread counts are ``result``-only, on purpose
        # — a failure report is not an unread reply); it is NOT reachable by message
        # search, which is also ``result``-scoped; and on a REMOTE-ACCESS install push can
        # find no owner to address, because owner resolution falls back to the local user
        # only while remote access is off (``core/web_push_notifications.py``). The inbox
        # card and the realtime event are unaffected by all three.
        #
        # WHEN. UNCONDITIONALLY, ONCE, LAST. One distinct workspace-notifications rung
        # after every person/context target, on every attempt, whether or not the four
        # rungs above produced anything and whether or not what they produced is stale,
        # unavailable, or failing delivery. This is the round-14 gate ruling (review
        # comment 5121007240) and it is a settled decision, not a preference to be
        # re-litigated by a later revision.
        #
        # TWO RETIRED DESIGNS, named so neither comes back:
        #
        # (i)  round 12's "ONLY WHEN NOTHING ELSE RESOLVED" — the fallback fired only for
        #      an empty ladder. Too narrow by exactly the case #1060 reported (maintainer
        #      note 5120451508): a NON-EMPTY ladder is not a DELIVERABLE one. Rung (5)
        #      above is manufactured from the run's session id and nothing checks that the
        #      row it names still exists, and rungs (3)/(4) can point at the same dead
        #      session. A watch bound to a HARD-DELETED session therefore builds a ladder
        #      that can never persist a receipt: every attempt sends to a candidate that
        #      resolves to nothing, the receipt-only ack source correctly refuses it, the
        #      ``if not rungs`` gate never fires, all six attempts burn, and the notice
        #      dead-letters into ``NOTICE_FAILED`` with nothing written anywhere. That
        #      silent dead letter is the 3.5 hours of silence in #1060's field evidence,
        #      and "visible in ``last_error``" was refuted above for exactly this reason.
        #
        # (ii) round 13's FINAL-ATTEMPT fallback (commit ``ce695b42``) — appended last,
        #      but only on attempt ``MAX_ATTEMPTS``. It closed (i)'s hole and was
        #      overruled anyway. Its argument was that a workspace rung present from
        #      attempt 1 converts a TRANSIENT preferred-rung failure into a permanently
        #      workspace-routed notice, since the walk acks the FIRST rung that succeeds.
        #      That reading of the mechanism is correct; the gate weighed the trade and
        #      decided the other way, in terms that leave no conditional room:
        #      "Workspace fallback is mandatory rung 5. Append one distinct
        #      workspace-notifications target after every person/context target, even
        #      when earlier candidates exist but are stale, unavailable, or fail
        #      delivery. It cannot be conditional on rungs being empty."
        #
        # THE TRADE, RECORDED RATHER THAN LEFT SILENT, because it is a real behaviour
        # change and the next reader deserves to find it stated: any walk in which every
        # preferred rung fails to deliver now reaches this rung on THAT walk, delivers,
        # and ACKS. So a preferred rung that failed TRANSIENTLY — a platform blip on
        # attempt 1 — permanently routes that one notice to the workspace inbox; the
        # notice is never retried to the preferred target, and the user's own channel
        # does not receive a copy it would have received a minute later. Accepted
        # explicitly by the maintainer in the ruling above ("…or fail delivery"), on the
        # judgement that a misrouted notice a user can find beats a notice nobody
        # receives. Scope of the residual: ONE notice, not the definition — the next
        # failure builds a fresh ladder and the recovered preferred rung wins it again.
        #
        # WHAT KEEPS IT FROM BEING NOISY is the ORDER, which is the one property of
        # round 12's argument that survives intact. The rung is appended STRICTLY LAST,
        # after every preferred rung, and the walk returns on the first rung that
        # acknowledges — so a HEALTHY preferred rung wins every walk and never produces a
        # workspace card at all. Duplicate cards for a delivered notice's silent per-rung
        # failures are therefore not reachable; only a walk where NOTHING preferred
        # delivered gets here. Pinned by
        # ``test_a_healthy_preferred_rung_never_reaches_the_workspace_inbox``.
        #
        # AND ONCE. The reserved id is appended as a CONSTANT, so it collides by identity
        # with rung (5)'s archived-session reroute (which returns the same constant) and
        # the ``_add`` seen-set collapses the two into a single rung. An archived-session
        # ladder holds exactly ONE workspace rung, not two — pinned by
        # ``test_an_archived_session_ladder_holds_exactly_one_workspace_rung``.
        #
        # NO DATABASE ACCESS HERE. Appending the constant costs nothing; the
        # resolve-or-create-or-heal is done by ``_emit_failure_notice`` when the walk
        # reaches this rung. That is what keeps the reserved row from being minted on the
        # first failure of an installation whose rung (1) always delivers — the only part
        # of round 12's "would create the reserved row for installations that never need
        # it" that is still load-bearing once the ordering argument above is granted.
        #
        # RESIDUAL: ``_workspace_notice_session_id`` returns ``None`` when the workbench
        # database cannot be read or written, and the walk then SKIPS this rung on every
        # attempt — so the notice dead-letters exactly as it did before, visibly, with the
        # last rung's own refusal in ``error``. That path must stay reachable: a fallback
        # that swallowed the dead letter when it could not itself deliver would replace
        # one silence with a worse one.
        _add(f"avibe::project::{WORKSPACE_NOTICE_SESSION_ID}", WORKSPACE_NOTICE_SESSION_ID)
        return rungs

    def _rung_five_session_id(self, session_id: str) -> str:
        """Which session rung (5) may address: the run's own, or the workspace inbox.

        THE INVISIBLE-SESSION REROUTE. An archived or background
        ``agent_sessions`` row is WRITABLE but is not a VISIBLE delivery surface,
        and the notice machinery reads those two as one thing:
        ``persist_agent_message`` resolves the avibe scope through ``_session_row``,
        which has no status filter, so the message persists; a persisted receipt is the
        workbench class's ack source (``LADDER_ACK_SOURCES``), so the rung ACKS and the
        notice is stamped ``sent`` and never retried; but ``list_inbox_sessions`` and
        ``get_inbox_session`` exclude archived sessions, so there is no inbox card, no
        ``inbox.session.updated`` patch for an open browser, and no Web Push. The user is
        told nothing and the system believes it told them — the exact silence D1 exists
        to close, and worse than a dead letter, which at least says so.

        The receipt-only ack source CANNOT catch this one. It is the right measurement
        for a MISSING row (nothing persists, so nothing acks — see
        ``_failure_notice_targets``' rung-(5) comment and
        ``test_an_avibe_rung_does_not_ack_on_a_synthetic_send_id``), and it is blind
        here precisely because the write really does happen. So the check has to be a
        read, and it has to be here, where the address is chosen.

        WHY REROUTING RATHER THAN HEALING, which is what round 12 did for the same class
        of hole (``2ceaa865``, the reserved workspace session). That row may never be
        archived at all — ``archive_session`` refuses its id — so an archived one is
        corruption and repairing it in place restores the invariant. An ORDINARY session
        was archived by its owner deliberately. Un-archiving it to deliver a failure
        notice would overrule a user decision to make a bookkeeping row visible, and it
        would resurface the whole session, not the notice. The session is not broken; the
        ADDRESS is. So the notice moves.

        Three outcomes, and only the middle one changes anything:

        * row ABSENT (hard-deleted, or the DB cannot be read): the caller's own
          candidate, unchanged. Nothing persists, so nothing acks, and the appended
          workspace rung plus the ``delivery_target_missing`` classification carry the
          case on the SAME walk. The candidate is
          also kept for what it documents: rung (5) is a candidate by construction, and
          removing it here would hide the shape of the ladder from the ack policy that
          depends on it.
        * row ARCHIVED OR HIDDEN: the reserved workspace-notifications session, so the
          notice lands somewhere ``list_inbox_sessions`` will show it. Returned as the
          bare CONSTANT — this method does not resolve or create the reserved row, so
          the reroute is a pure address change and the id it returns is identical to the
          one ``_failure_notice_targets`` appends last, which is what lets the
          ``_add`` seen-set collapse the two into ONE rung. Whether that row can
          actually be written is settled later, by the walk. If it cannot, the rung is
          skipped and the notice stays retryable rather than acking invisibly into the
          archived row — which is what keeping the caller's candidate here used to do.
        * an active row whose visibility is explicitly admitted by the inbox: the
          caller's candidate. The visibility whitelist is shared with inbox queries,
          so a future value stays hidden until both surfaces opt it in.

        ONE READ, and the TOCTOU is real and bounded: a session archived or hidden
        between this SELECT and ``persist_agent_message``'s write still acks there.
        The residual is ONE notice, not a class — the next notice re-reads and reroutes —
        and closing it would need the status check inside the persisting transaction,
        which is a writer-side change of a different shape. Not worth trading a
        per-notice window for a widened writer.
        """

        try:
            with get_cached_sqlite_engine().begin() as conn:
                row = conn.execute(
                    select(agent_sessions.c.status, agent_sessions.c.visibility).where(
                        agent_sessions.c.id == session_id
                    )
                ).first()
        except Exception:
            logger.warning(
                "failure notice: cannot read session %s status/visibility for rung (5)",
                session_id,
                exc_info=True,
            )
            return session_id
        if row is None:
            return session_id
        status, visibility = row
        if str(status) != "archived" and str(visibility) in INBOX_SESSION_VISIBILITIES:
            return session_id
        logger.info(
            "failure notice: session %s is archived or hidden, rerouting rung (5) to the "
            "workspace-notifications inbox",
            session_id,
        )
        return WORKSPACE_NOTICE_SESSION_ID

    def _workspace_notice_session_id(self) -> Optional[str]:
        """The reserved workspace-notifications session id, created on first need.

        Lazy on purpose: an installation whose definitions all have a delivery key or a
        session never grows the row. See
        ``storage.agent_session_rows.resolve_workspace_notice_session`` for why the
        identity is a reserved primary key and why the row carries no Scope.

        THREE MECHANISMS KEEP THE ROW USABLE, and they cover three different ways of
        losing it — the first one alone was not enough:

        * RECREATION covers REMOVAL. Nothing asks the ``/new`` clear path or session
          eviction for an exemption; if the row is deleted the next notice makes it again.
        * HEALING covers ARCHIVE and a flipped ``visibility``, which recreation cannot
          see: the reserved primary key is still there, so nothing recreates, while the
          row has stopped being a delivery surface. That state fails SILENTLY — the
          notice still persists through ``_session_row`` (no status filter), still acks
          on the receipt, and ``list_inbox_sessions`` shows nothing — so
          ``resolve_workspace_notice_session`` repairs the row in place instead.
        * THE ``archive_session`` AND ``update_session`` GUARDS cover the UI paths.
          The row is ``visibility='system'`` — absent from ordinary session lists,
          admitted by the inbox surfaces — so the remaining door to it is its own
          inbox card, and ``storage.workbench_sessions_service`` refuses the id on
          both archive and modify (403). The heal is still needed for a database
          archived or re-flagged out of band, or before the guards existed.

        Called from the WALK (``_emit_failure_notice``), not from the ladder build. The
        ladder appends the reserved id as a constant, so this — the only part of the rung
        that writes — runs exactly when the walk has actually reached it. An install
        whose preferred rung always delivers therefore never grows the row.

        Returns ``None`` rather than raising: this runs mid-walk for a failure that is
        already recorded, and an unwritable workbench DB must leave the notice retryable
        — the pre-existing behaviour — instead of turning one unusable rung into an
        exception the drain has to classify.
        """

        try:
            from storage.agent_session_rows import resolve_workspace_notice_session
            from storage.db import get_cached_sqlite_engine

            with get_cached_sqlite_engine().begin() as conn:
                return resolve_workspace_notice_session(
                    conn,
                    # Named once, at CREATE time, by whoever's notice needed it first.
                    title=self._t("harness.notice.workspaceSession"),
                )
        except Exception:
            logger.warning(
                "failure notice: workspace-notifications session unavailable", exc_info=True
            )
            return None

    def _origin_lines(self, caller: Optional[dict[str, Any]]) -> list[str]:
        """The creation-origin line, plus a deep link when one can be built honestly.

        Zero, one or two lines — never a placeholder. Three separate refusals, each of
        which independently yields FEWER lines rather than vaguer ones:

        * **No provenance at all.** Every definition created before the origin capture
          landed, and every definition created from the CLI with no conversation behind
          it. There is nothing to say, so nothing is said.
        * **An unmapped platform.** The label is rendered inside a translated sentence,
          so the wire value goes through ``NOTICE_ORIGIN_PLATFORM_I18N_KEYS`` — a closed
          map — and never gets interpolated. Same call as
          ``notice_failure_class_i18n_key``'s: ``None`` means no line, because "Created
          in: mystery_platform" leaks an identifier into product copy for no benefit.
        * **No followable link.** ``origin_link`` returns ``None`` for a Feishu/Lark or
          WeChat origin, for a Workbench origin (a localhost URL is not reachable from
          the IM notice this may be delivered to), and for any platform whose permalink
          grammar needs an id that was not captured. The origin TEXT still renders; only
          the second line is dropped.

        The channel and thread are rendered from the CAPTURED ids — a raw ``C0123`` is
        the identifier the user's own client shows in a URL, and inventing a display
        name that was never captured would be a different kind of dishonesty from
        inventing a link, but the same kind of mistake. The scope is read back out of
        the same ``session_key`` that rung (3) is addressed to, so the notice cannot
        name one conversation while the ladder targets another.
        """

        if not caller:
            return []
        platform = str(caller.get("platform") or "").strip()
        platform_key = failure_notices.notice_origin_platform_i18n_key(platform)
        if not platform_key:
            return []
        platform_label = self._t(platform_key)

        parsed: Optional[ParsedSessionKey] = None
        raw_key = str(caller.get("session_key") or caller.get("scope_id") or "").strip()
        if raw_key:
            for parser in (parse_session_key, parse_scope_id):
                try:
                    parsed = parser(raw_key)
                    break
                except Exception:
                    parsed = None
        scope_type = parsed.scope_type if parsed is not None else ""
        scope_id = parsed.scope_id if parsed is not None else ""
        thread_id = parsed.thread_id if parsed is not None else None

        if scope_type == "channel" and scope_id:
            if thread_id:
                origin = self._t(
                    "harness.notice.originChannelThread",
                    platform=platform_label,
                    channel=scope_id,
                    thread=thread_id,
                )
            else:
                origin = self._t(
                    "harness.notice.originChannel",
                    platform=platform_label,
                    channel=scope_id,
                )
        elif scope_type == "user" and scope_id:
            origin = self._t(
                "harness.notice.originDirect",
                platform=platform_label,
                user=scope_id,
            )
        else:
            # A known platform with no usable scope — a Workbench (``project``) origin,
            # or a caller recorded before the session key could be resolved. The
            # platform alone is still true and still narrows the search.
            origin = platform_label

        lines = [self._t("harness.notice.origin", origin=origin)]
        link = origin_link(
            platform,
            caller.get("channel_id"),
            thread_id,
            caller.get("message_id"),
            caller.get("workspace_id"),
        )
        if link:
            lines.append(self._t("harness.notice.originLink", url=link))
        return lines

    def _failure_notice_body(self, run: dict[str, Any], notice: dict[str, Any]) -> str:
        """Actionable copy: what failed, why, its state, and how to re-run.

        A DM is context-free by construction and rung (5) is not attached to any
        conversation, so the body has to carry its own context rather than relying on
        where it happened to land.

        Two classifications decide the copy, and BOTH are asked here by the same
        predicate the rest of the system uses:

        * the LANE — ``failure_notices.is_interruption``, i.e. membership in
          ``RUN_INTERRUPTION_REASONS``. Asking by the mere presence of
          ``interrupt_reason`` told a user "nothing is wrong with the definition
          itself" for ``no_terminal_result`` / ``refused_concurrent_turn`` /
          ``transport_unavailable`` / ``queue_hold_expired`` — the recurring per-fire
          verdicts where the definition is exactly what IS wrong.
        * the DEFINITION KIND. A watch is not a task: ``vibe task run`` /
          ``vibe task show`` do not accept a watch id, and ``vibe watch run`` does not
          exist at all, so a failed watch was handed commands it could not use and an
          id in place of its name (``get_task`` mirrors scheduled tasks only).
        """

        definition_id = str(run.get("task_id") or "") or None
        task = self.store.get_task(definition_id) if definition_id else None
        # Only for a run whose definition is not a task: the run's own ``run_type``
        # says so for a watch hook send (``watch``) and for the supervisor heartbeat
        # (``watch_runtime``), and the definition row is the fallback for a row
        # rebuilt without one.
        watch = (
            self.store.get_watch_definition(definition_id)
            if task is None and definition_id
            else None
        )
        is_watch = watch is not None or str(run.get("run_type") or "").strip().startswith("watch")
        explicit_name = (task.name if task else None) or (
            str((watch or {}).get("name") or "").strip() or None
        )
        name = (
            explicit_name
            or definition_id
            or str(run["id"])
        )
        run_id = str(run.get("id") or "").strip()
        if failure_notices.is_binding_change(notice):
            return self._binding_notice_body(
                notice,
                name=name,
                definition_id=definition_id,
                definition_exists=task is not None,
                run_id=run_id,
            )
        reason = str(notice.get("interrupt_reason") or "").strip()
        error = str(run.get("error") or "").strip() or self._t("harness.notice.unknownError")
        if reason == SETTLED_BY_RESTARTED:
            # A service restart is a lifecycle event, not a diagnosis. Keep its
            # notification to one calm action and leave internal Run/definition
            # details on the inspection surfaces. A deleted definition has no
            # trustworthy display name, so never substitute its opaque id.
            if explicit_name:
                return self._t("harness.notice.restartStopped", name=explicit_name)
            return self._t("harness.notice.restartStoppedUnnamed")
        if failure_notices.is_interruption(notice):
            # The reason is rendered INSIDE a translated sentence, so it is copy: the
            # wire value went through a closed label map, never interpolated raw. An
            # unmapped reason takes the map's localized generic rather than leaking the
            # identifier — see ``NOTICE_REASON_UNKNOWN_I18N_KEY``.
            headline = self._t(
                "harness.notice.interrupted",
                name=name,
                reason=self._t(failure_notices.notice_reason_i18n_key(reason)),
            )
        elif is_watch and str(
            ((run.get("metadata") or {}).get(WATCH_HOOK_OUTCOME_METADATA_KEY) or "")
        ).strip() == WATCH_HOOK_OUTCOME_CIRCUIT_REPAIR:
            if watch is not None and not watch.get("enabled") and not watch.get(
                "retired_at"
            ):
                headline = self._t(
                    "harness.notice.watchCircuitRepairFailed",
                    name=name,
                )
            else:
                headline = self._t("harness.notice.watchFollowUpFailed", name=name)
        elif is_watch and str(
            ((run.get("metadata") or {}).get(WATCH_HOOK_OUTCOME_METADATA_KEY) or "")
        ).strip() == WATCH_HOOK_OUTCOME_EVENT:
            headline = self._t("harness.notice.watchProcessingFailed", name=name)
        elif is_watch and str(
            ((run.get("metadata") or {}).get(WATCH_HOOK_OUTCOME_METADATA_KEY) or "")
        ).strip() == WATCH_HOOK_OUTCOME_WAITER_FAILURE:
            headline = self._t("harness.notice.watchFailureReportFailed", name=name)
        elif is_watch:
            headline = self._t("harness.notice.watchFollowUpFailed", name=name)
        else:
            headline = self._t("harness.notice.failed", name=name)
        # COMMAND COPY, ORDINARY-FAILED LANE ONLY. A command definition's notice named
        # the definition and the error but never the command, so "Error: command exited
        # with status 7: boom" left the reader to go and look up WHAT exited. The lane
        # gate is deliberate: an interrupted command run is a fact about the SERVICE
        # (restart, eviction) and not about the command, so that lane keeps the generic
        # copy the headline already explains.
        #
        # The command copy is read off the RUN's own snapshot, falling back to the live
        # definition only for rows written before the snapshot existed. This notice is
        # drained asynchronously, so the definition can have been rewritten -- or
        # deleted -- between the fire and this call; either way ``get_task`` answers
        # about the wrong execution, or not at all.
        executed = command_snapshot_of_run(run)
        command_source = executed if executed is not None else task
        is_command_failure = (
            not failure_notices.is_interruption(notice)
            and command_source is not None
            and command_source.has_command
        )
        lines = [headline]
        if is_command_failure:
            command_preview = self._notice_command_preview(command_source)
            if command_preview:
                lines.append(self._t("harness.notice.commandLine", command=command_preview))
        lines.append(self._t("harness.notice.error", error=error))
        if is_command_failure and run.get("exit_code") is not None:
            # ONLY when the row actually carries one. A fire that never spawned — a
            # missing working directory, a supervisor that died during startup — has no
            # exit code at all, and "Exit code: None" (or a fabricated 0/1) would be
            # copy about nothing on the very notice that has to be trusted. No stderr
            # tail line either: the executor already appends the last non-empty stderr
            # line to the run's ``error``, so the line above carries it and a third
            # line would print the same text twice.
            lines.append(self._t("harness.notice.commandExit", exitCode=run.get("exit_code")))
        if not failure_notices.is_interruption(notice):
            # D5 asks for "the error and its CLASS", and on this lane the class was
            # dropped: the interrupted headline was the only place any reason was
            # rendered, while the per-fire verdicts — ``no_terminal_result``,
            # ``refused_concurrent_turn``, ``transport_unavailable``,
            # ``queue_hold_expired`` — stay in the FAILED lane by design and carry a
            # reason all the same. Its own closed vocabulary, and ``None`` (no line)
            # when there is no class to name: see ``notice_failure_class_i18n_key``.
            class_key = failure_notices.notice_failure_class_i18n_key(reason)
            if class_key:
                lines.append(
                    self._t("harness.notice.failureClass", failureClass=self._t(class_key))
                )
        last_success = self._last_success_instant(definition_id)
        if last_success:
            # "When it last succeeded" — D5's own list. Omitted rather than rendered as
            # "never" for a definition that has never succeeded: the notice already says
            # this fire failed, and a line about the absence of history is noise.
            lines.append(self._t("harness.notice.lastSucceeded", when=last_success))
        # WHERE IT CAME FROM — the last item on D5's list, and the one a DM or a
        # workspace card needs most, because neither is attached to the conversation
        # that asked for the definition. Omitted whole when nothing was captured, which
        # is EVERY definition created before this round: there is no backfill and no
        # migration, because the ids were never recorded and inventing them is the one
        # thing a provenance line may not do.
        lines.extend(
            self._origin_lines(
                _created_by_caller(task, run.get("metadata") if isinstance(run.get("metadata"), dict) else {})
            )
        )
        if definition_id:
            lines.append(self._t("harness.notice.definition", id=definition_id))
            if task is None and watch is None:
                # The definition row is GONE while the run keeps its ``definition_id``
                # forever, so EVERY definition-level command is a dead end — checked
                # before the task/watch split because both halves of that split print
                # one. ``vibe task run <deleted id>`` parses and then reports "not
                # found", which is the same class of defect HFR-094 closed for
                # watches: copy naming an action the user cannot take.
                lines.extend(self._deleted_definition_lines(run_id))
            elif is_watch:
                if watch is not None and not watch.get("enabled", True):
                    # RETIRED IS NOT PAUSED, and the row already says which. A watch
                    # that ran out its ``once`` cycle carries ``retired_at``; a user who
                    # pressed pause leaves it null. Both land on ``enabled = 0``, which
                    # is exactly the ambiguity #1060 reported ("``enabled=0`` is carrying
                    # three meanings") — and printing the resume copy for a retired
                    # watch contradicts the lifecycle projection this same round makes
                    # authoritative: the definition reads FINISHED while its notice
                    # offers ``vibe watch resume``, an action that would arm a watch the
                    # user never paused.
                    #
                    # The distinction is read from the same column the projection's
                    # ``ended`` predicate uses, so the copy and the badge cannot
                    # disagree. A legacy row with no marker keeps the resume copy: its
                    # history genuinely cannot prove which of the two happened, and
                    # ``definition_lifecycle_expression`` makes the same call.
                    if str(watch.get("retired_at") or "").strip():
                        if failure_notices.is_interruption(notice):
                            lines.append(self._t("harness.notice.watchRetired"))
                    else:
                        lines.append(self._t("harness.notice.watchPaused", id=definition_id))
                # No re-run affordance, because there is no ``vibe watch run``: a watch
                # fires when the thing it waits on happens. ``show`` is the action a
                # user actually has.
                lines.append(self._t("harness.notice.watchShow", id=definition_id))
            else:
                if task is not None and not task.enabled:
                    # FINISHED IS NOT PAUSED, the task-side twin of the watch branch
                    # above. A failed one-shot is disabled by ``mark_task_result``
                    # (``disable_one_shot``), so ``enabled = 0`` here carries two
                    # meanings — and the paused copy offers ``vibe task resume`` for
                    # a definition the canonical lifecycle projection reads as
                    # FINISHED, an action that re-arms nothing. The distinction is
                    # read from the projection ITSELF (``definition_lifecycle_state``
                    # evaluates the badge's own CASE for this row) rather than
                    # re-derived in Python, so the copy and the badge cannot
                    # disagree — not even in the window where a naive ``run_at`` in
                    # a non-UTC task timezone reads differently to
                    # ``compute_next_run_at`` and to SQLite's UTC clock. The
                    # explicit re-run affordance below stays either way: unlike a
                    # watch, ``vibe task run`` is real and is the honest next step.
                    lifecycle = self._task_lifecycle_state(definition_id, task)
                    if lifecycle == "finished":
                        lines.append(self._t("harness.notice.taskFinished"))
                    elif lifecycle == "paused":
                        lines.append(self._t("harness.notice.paused", id=definition_id))
                    # ``running`` (``vibe task run`` accepts a disabled one-shot, and
                    # an in-flight execution outranks the ended predicate) and
                    # ``waiting`` (a switch the mirror has not caught up with): no
                    # lifecycle line at all. Either copy would contradict the badge,
                    # and the re-run/show affordance below stands on its own.
                elif task is not None:
                    next_run = compute_next_run_at(
                        enabled=task.enabled,
                        schedule_type=task.schedule_type,
                        cron=task.cron,
                        run_at=task.run_at,
                        timezone_name=task.timezone,
                    )
                    if next_run:
                        lines.append(self._t("harness.notice.nextRun", when=next_run))
                lines.append(self._t("harness.notice.rerun", id=definition_id))
        return "\n".join(lines)

    @staticmethod
    def _notice_command_preview(task: Any) -> str:
        """The one-line command a notice names, or ``""`` for a non-command definition.

        Reads whichever shape the caller has -- a ``ScheduledTask`` or the run's own
        snapshot -- and hands both to the shared ``command_line_preview``, so the
        command in a failure notice is the same string ``vibe task list`` showed.
        """

        return command_line_preview(
            getattr(task, "shell_command", None), getattr(task, "command", None)
        )

    def _task_lifecycle_state(self, definition_id: Optional[str], task: Any) -> str:
        """The lifecycle state the badge shows for this task — asked of the badge.

        The authoritative answer is ``definition_lifecycle_state``, the same SQL CASE
        every list and count surface evaluates. An in-flight execution outranks the
        persisted terminal marker, so the caller sees ``running`` while a consumed Run
        is active. The Python fallback for the file backend follows the same facts.
        """

        if definition_id:
            store = getattr(self.request_store, "_sqlite", None)
            if store is not None:
                try:
                    state = store.definition_lifecycle_state(
                        definition_id, definition_type="task"
                    )
                except Exception:
                    logger.debug(
                        "failure notice: lifecycle-state read failed", exc_info=True
                    )
                    state = None
                if state is not None:
                    return state
        if task.schedule_type == "at" and task.retired_at is not None:
            return "finished"
        return "paused"

    def _last_success_instant(self, definition_id: Optional[str]) -> Optional[str]:
        """When this definition last succeeded, for the body's own context.

        Read through the request store's SQLite handle, which is where run history lives
        (the task mirror holds definitions, not runs). ``None`` on the file backend, on a
        run with no definition, and on any read error: this is one context line on a
        notice that must still be delivered, so an unanswerable question drops the line
        rather than the notice.
        """

        if not definition_id:
            return None
        store = getattr(self.request_store, "_sqlite", None)
        if store is None:
            return None
        try:
            return store.last_success_settled_at(definition_id)
        except Exception:
            logger.debug("failure notice: last-success read failed", exc_info=True)
            return None

    def _deleted_definition_lines(self, run_id: str) -> list[str]:
        """The only recovery copy a definition that no longer exists can honestly print.

        One place, because both bodies need it and both had the same hole. The run row
        outlives its definition, so the RUN is what is left to inspect — and
        ``vibe runs show`` is a real subcommand with an optional positional run id,
        vetted against the real parser by
        ``test_a_deleted_definition_notice_names_only_run_level_recovery``.

        No run id (a body rendered from a row rebuilt without one) prints the
        explanation alone rather than ``vibe runs show`` with nothing after it: an
        incomplete command is the WI-2 failure mode again, one argument smaller.
        """

        lines = [self._t("harness.notice.definitionDeleted")]
        if run_id:
            lines.append(self._t("harness.notice.runInspect", id=run_id))
        return lines

    def _binding_notice_body(
        self,
        notice: dict[str, Any],
        *,
        name: str,
        definition_id: Optional[str],
        definition_exists: bool,
        run_id: str,
    ) -> str:
        """Copy for "your pinned session was replaced", which is not a failure report.

        Two things the failure body must not do here. It opens with "failed" and
        always prints an ``Error:`` line — for a run that SUCCEEDED that reads as a
        false alarm, and with ``error=None`` the line degrades to "no error text was
        recorded", which is noise about nothing. And its call to action is ``vibe task
        run``, whereas the action a user actually wants after an unrequested rebind is
        to pin the session back or look at what the definition is bound to now.

        Every command named below is a real subcommand (``vibe task update
        --session-id``, ``vibe task show``); the WI-2 lesson is that invented copy
        fails nothing but the user. ``definition_exists`` is the second half of that
        lesson: a rebind notice can outlive its definition by the whole retry/backoff
        window, and a command that names a row which no longer exists is invented copy
        by a slower route — it parses, and then reports "not found".
        """

        binding = notice.get("binding") if isinstance(notice.get("binding"), dict) else {}
        previous = str(binding.get("previous_session_id") or "").strip()
        new = str(binding.get("new_session_id") or "").strip()
        lines = [self._t("harness.notice.rebound", name=name)]
        if previous and new:
            lines.append(self._t("harness.notice.reboundSessions", previous=previous, new=new))
        if binding.get("settings_preserved"):
            lines.append(self._t("harness.notice.reboundSettingsPreserved"))
        else:
            lines.append(self._t("harness.notice.reboundSettingsReset"))
        if definition_id:
            lines.append(self._t("harness.notice.definition", id=definition_id))
            if definition_exists:
                lines.append(self._t("harness.notice.reboundRepin", id=definition_id))
                lines.append(self._t("harness.notice.show", id=definition_id))
            else:
                # Nothing left to re-pin OR to show. The rebind itself still happened
                # and the lines above still report it — the news is not suppressed,
                # only the actions that no longer address anything.
                lines.extend(self._deleted_definition_lines(run_id))
        return "\n".join(lines)

    def settle_activity_runs(self, activity: Any) -> list[str]:
        """Settle deferred Runs when a failed/stopped owned Activity is last."""

        activity_status = str(getattr(activity, "status", "") or "").strip().lower()
        if activity_status == "completed":
            # A completed Claude task may still produce a user-visible follow-up;
            # that Message owns Run settlement so output and callback stay aligned.
            return []
        terminal_status = "failed" if activity_status == "failed" else "canceled"
        metadata = getattr(activity, "metadata", None) or {}
        run_ids: list[str] = []
        primary = str(getattr(activity, "run_id", "") or "").strip()
        if primary:
            run_ids.append(primary)
        values = metadata.get("run_ids") if isinstance(metadata, dict) else None
        if isinstance(values, list):
            for value in values:
                run_id = str(value or "").strip()
                if run_id and run_id not in run_ids:
                    run_ids.append(run_id)

        registry = getattr(getattr(self.controller, "agent_service", None), "activities", None)
        has_blocker = getattr(registry, "has_blocking_run_activity", None)
        has_pending_output = getattr(registry, "has_pending_run_output", None)
        settled: list[str] = []
        for run_id in run_ids:
            self.request_store.defer_run_terminal(
                run_id,
                terminal_status=terminal_status,
            )
            if callable(has_blocker) and has_blocker(run_id):
                continue
            if callable(has_pending_output) and has_pending_output(run_id):
                continue
            error = f"Background Activity {getattr(activity, 'id', '')} {activity_status}"
            if self.request_store.settle_deferred_run(
                run_id,
                error=error,
            ):
                settled.append(run_id)
        if settled:
            self._wake_runtime_work(
                RuntimeWorkLane.REQUESTS,
                RuntimeWorkLane.RUN_CALLBACKS,
                RuntimeWorkLane.FAILURE_NOTICES,
            )
        return settled

    async def _drain_vault_callbacks(self) -> None:
        """Auto-resume the requesting session when a vault request reaches a terminal state.

        Mirrors :meth:`_drain_callbacks` but for ``vault_requests`` (which resolve outside the run
        store). Pending rows use the same processor as the runtime-work lane, so callbacks either
        enqueue one turn or mirror a completed waiter's outcome directly into the transcript before
        being marked ``sent``/``skipped``/``failed``. Delivery is at-least-once; enqueue or mirror
        and the ``sent`` mark are separate writes, with stable identities closing duplicate windows.
        """
        if not self._owns_service_instance():
            return
        from storage import vault_service

        # Runs every tick, so use the process-local cached engine (never dispose it) rather than
        # allocating a fresh engine per sweep.
        engine = get_cached_sqlite_engine(paths.get_sqlite_state_path())
        try:
            with engine.begin() as conn:
                # Expiry is lazy (only on request reads), so proactively expire overdue pending
                # requests here — otherwise an unattended timed-out request would never arm its
                # callback until some unrelated read touched it. Both happen in one pass.
                vault_service.expire_overdue_requests(conn)
                pending = vault_service.list_pending_request_callbacks(conn)
        except Exception as exc:
            logger.error("Vault request callback sweep failed to load: %s", exc, exc_info=True)
            return
        for row in pending:
            # Keep the legacy drain aligned with the runtime-work processor, including completed
            # waiter outcomes that belong in the transcript without starting another Agent turn.
            status = self._process_vault_callback_sync(row)
            if status == "sent":
                # A callback run was enqueued into the run store; drain it promptly.
                self._wake_runtime_work(RuntimeWorkLane.REQUESTS)

    def _scan_vault_callback_work(
        self,
        *,
        limit: int,
        occupied: frozenset[str],
        cursor: str | None,
    ) -> tuple[list[RuntimeWorkItem], bool]:
        from storage import vault_service

        engine = get_cached_sqlite_engine(paths.get_sqlite_state_path())
        after = None
        if cursor:
            parts = cursor.split("\x1f", 1)
            if len(parts) == 2:
                after = (parts[0], parts[1])
        with engine.begin() as conn:
            vault_service.expire_overdue_requests(conn)
            next_expiry = vault_service.earliest_pending_request_expiry(conn)
            pending = vault_service.list_pending_request_callbacks(
                conn,
                limit=limit,
                after=after,
            )
        if next_expiry is not None:
            self._schedule_runtime_work_wake(
                RuntimeWorkLane.VAULT_CALLBACKS,
                max(
                    0.0,
                    (next_expiry - datetime.now(timezone.utc)).total_seconds(),
                ),
            )
        items: list[RuntimeWorkItem] = []
        for row in pending:
            request_id = str(row.get("id") or "")
            if not request_id:
                continue
            try:
                plan = vault_service.resolve_request_callback(row)
            except Exception:
                plan = None
            session_id = str(getattr(plan, "session_id", "") or "")
            partition = f"session:{session_id}" if session_id else f"request:{request_id}"
            items.append(
                RuntimeWorkItem(
                    partition,
                    row,
                    cursor_key=f"{row.get('decided_at') or ''}\x1f{request_id}",
                )
            )
        return items, len(pending) >= limit

    async def _process_vault_callback(self, row: dict[str, Any]) -> bool:
        status = await self._run_runtime_sync(self._process_vault_callback_sync, row)
        if status == "sent":
            self._wake_runtime_work(RuntimeWorkLane.REQUESTS)
        return status != "pending"

    def _process_vault_callback_sync(self, row: dict[str, Any]) -> str:
        from storage import vault_service

        request_id = str(row.get("id") or "")
        if not request_id:
            return "skipped"
        engine = get_cached_sqlite_engine(paths.get_sqlite_state_path())
        status = "skipped"
        try:
            with engine.begin() as conn:
                ready = vault_service.request_callback_ready(conn, row)
            if not ready:
                return "pending"
            plan = vault_service.resolve_request_callback(row)
            if plan is not None:
                request_type = str(row.get("request_type") or "")
                request_status = str(row.get("status") or "")
                if vault_service.request_callback_consumed_by_waiter(row):
                    from core.message_mirror import mirror_vault_waiter_outcome

                    mirrored = mirror_vault_waiter_outcome(
                        session_id=plan.session_id,
                        request_id=request_id,
                        request_type=request_type,
                        request_status=request_status,
                        message=plan.message,
                    )
                    if not mirrored:
                        # The stable native message id makes this mirror safe to retry. Keep the
                        # callback pending so a transient SQLite/dispatch failure is retried by
                        # the next sweep instead of losing the waiter's transcript outcome.
                        logger.warning("failed to persist Vault waiter outcome for %s; will retry", request_id)
                        return "pending"
                else:
                    enqueue_session_callback(
                        self.request_store,
                        session_id=plan.session_id,
                        message=plan.message,
                        source_actor=f"vault:{request_id}",
                        metadata={
                            "vault_request_type": request_type,
                            "vault_request_status": request_status,
                        },
                    )
                status = "sent"
        except ValueError:
            status = "skipped"
        except Exception as exc:
            logger.error(
                "Vault request callback failed for %s: %s",
                request_id,
                exc,
                exc_info=True,
            )
            status = "failed"
        try:
            with engine.begin() as conn:
                vault_service.mark_request_callback(conn, request_id, status=status)
        except Exception as exc:
            logger.error(
                "Vault request callback mark failed for %s: %s",
                request_id,
                exc,
                exc_info=True,
            )
            return "pending"
        return status

    def _enqueue_callback_run(self, run: dict[str, Any]) -> Optional[TaskExecutionRequest]:
        callback_session_id = str(run.get("callback_session_id") or "").strip()
        if not callback_session_id:
            return None
        run_id = str(run.get("id") or "")
        status = _normalize_requested_run_status(run.get("status")) or str(
            run.get("status") or ""
        )
        run_metadata = run.get("metadata")
        terminal_turn_id = (
            str(run_metadata.get("turn_id") or "").strip()
            if isinstance(run_metadata, dict)
            else ""
        )
        callback_metadata = (
            {CALLBACK_TERMINAL_TURN_ID_METADATA_KEY: terminal_turn_id}
            if terminal_turn_id
            else None
        )
        if status in {"failed", "canceled"}:
            terminal_message = self._fallback_callback_result(run, status=status)
            terminal_callback = enqueue_session_callback(
                self.request_store,
                session_id=callback_session_id,
                message=terminal_message,
                source_actor=f"{run_id}:terminal:{status}",
                source_session_id=str(run.get("session_id") or "").strip() or None,
                parent_run_id=run_id or None,
                metadata=callback_metadata,
            )
            if terminal_callback is not None:
                return terminal_callback
        return enqueue_session_callback(
            self.request_store,
            session_id=callback_session_id,
            message=self._build_callback_message(run),
            source_actor=run_id,
            source_session_id=str(run.get("session_id") or "").strip() or None,
            parent_run_id=run_id or None,
            metadata=callback_metadata,
        )

    def _build_callback_message(self, run: dict[str, Any]) -> str:
        status = _normalize_requested_run_status(run.get("status")) or str(run.get("status") or "")
        result_text = str(run.get("result_text") or "").strip()
        if not result_text:
            result_text = self._fallback_callback_result(run, status=status)
        return result_text.strip()

    def _fallback_callback_result(self, run: dict[str, Any], *, status: str) -> str:
        parts: list[str] = []
        if run.get("error"):
            parts.append(f"Error: {run['error']}")
        if run.get("stderr"):
            parts.append(str(run["stderr"]))
        if run.get("stdout") and status != "succeeded":
            parts.append(str(run["stdout"]))
        if parts:
            return "\n\n".join(part.strip() for part in parts if part and part.strip())
        if status == "canceled":
            return self._t("harness.run.fallbackResult.canceled")
        if status == "failed":
            return self._t("harness.run.fallbackResult.failed")
        return ""

    def _execution_lock_key(self, request: TaskExecutionRequest) -> Optional[str]:
        """Canonical conversation identity for per-session single-flight.

        Resolves task-only and session-id-only requests down to one canonical
        key so any two requests targeting the same conversation serialize,
        regardless of which identifier form they carry:

        - ``scheduled``/``task_run`` rows may carry only a ``task_id``; the
          real target lives on the task definition (mirrors
          ``_execute_claimed_request``).
        - a ``session_id`` is resolved to its canonical session key, so it
          matches a legacy/watch run that only carries that ``session_key``.

        Returns ``None`` for ``create_per_run`` (fresh session each time) and
        unkeyable requests.

        A COMMAND FIRE IS NOT A TURN, so it never takes a conversation's key. The lock
        exists to stop two Agent turns running in one conversation at once; a command
        talks to no Agent and holds no native session, yet it is bound to one (an
        ``--on-failure agent`` definition must be), and ``--timeout`` defaults to six
        hours -- so inheriting that key let one long backup command leave every queued
        turn in the conversation skipped as ``session_busy`` for as long as it ran. Its
        escalation IS a turn, and that is a separate run which takes the lock in the
        ordinary way. Keyed on the definition rather than dropped to ``None`` so a fire
        is still serialized against itself.
        """
        session_policy = request.session_policy
        session_id = request.session_id
        session_key = request.session_key
        task_id = request.task_id
        if request.request_type in {"task_run", "scheduled"} and task_id:
            task = self.store.get_task(task_id)
            if task is not None:
                if task.has_command:
                    return f"task:{task_id}"
                session_policy = task.session_policy or session_policy
                session_id = task.session_id or session_id
                session_key = task.session_key or session_key
        if session_policy == "create_per_run":
            return None
        if session_id:
            return self._canonical_session_lock(session_id, session_key)
        if session_key:
            return self._normalize_session_key(session_key)
        if task_id:
            return f"task:{task_id}"
        return None

    def _request_target_platform(self, request: TaskExecutionRequest) -> Optional[str]:
        session_key = request.session_key
        session_id = request.session_id
        deliver_key = request.deliver_key
        metadata = request.metadata or {}
        if request.request_type in {"task_run", "scheduled"} and request.task_id:
            task = self.store.get_task(request.task_id)
            if task is not None:
                session_key = task.session_key or session_key
                session_id = task.session_id or session_id
                deliver_key = task.deliver_key or deliver_key
                metadata = task.metadata or metadata

        if session_id:
            return resolve_session_id_target(session_id).session_key.platform
        if session_key:
            try:
                return parse_session_key(session_key).platform
            except ValueError:
                return parse_scope_id(session_key).platform

        scope_id = str(metadata.get("session_scope_id") or "").strip()
        if scope_id:
            return parse_scope_id(scope_id).platform
        if deliver_key:
            try:
                return parse_session_key(deliver_key).platform
            except ValueError:
                return parse_scope_id(deliver_key).platform
        return None

    def _transport_ready_for_request(self, request: TaskExecutionRequest) -> bool:
        if request.request_type in {"task_run", "scheduled"} and request.task_id:
            task = self.store.get_task(request.task_id)
            if task is not None and task.has_command:
                # A command run needs no IM transport: it produces an exit code and
                # output, not a reply. Gating it on a platform's readiness would hold a
                # backup or health check queued while an unrelated IM adapter is down.
                # The failure-notice path owns delivery, and it can be owed later.
                return True
        try:
            platform = self._request_target_platform(request)
        except Exception:
            logger.debug("Could not resolve Run %s platform for readiness gating", request.id, exc_info=True)
            return True
        if not platform:
            return True
        is_ready = getattr(self.controller, "is_im_transport_ready", None)
        return bool(is_ready(platform)) if callable(is_ready) else True

    def notify_transport_ready(self, platform: str) -> None:
        logger.info("Transport %s ready; scheduled Run queue will be drained", platform)
        supervisor = getattr(self.controller, "runtime_work_supervisor", None)
        notify = getattr(supervisor, "notify", None)
        if callable(notify):
            notify(RuntimeWorkLane.REQUESTS, reset_cursor=True)
            return
        self._drain_dirty = True

    def _canonical_session_lock(self, session_id: str, session_key: Optional[str]) -> str:
        cached = self._session_lock_cache.get(session_id)
        if cached is not None:
            return cached
        try:
            resolved = resolve_session_id_target(session_id)
            # avibe/workbench sessions are 1:1 with the session id — a project scope
            # holds many INDEPENDENT sessions, so locking on the project key would
            # serialize unrelated conversations. Lock on the concrete session id.
            if resolved.session_key.platform == "avibe" or resolved.session_key.scope_type == "project":
                key = f"sid:{session_id}"
            else:
                key = f"key:{resolved.session_key.to_key()}"
        except Exception:
            # avibe/web sessions (no IM scope) or unresolved ids: fall back to a
            # carried session key if present, else the id is its own identity.
            key = self._normalize_session_key(session_key) if session_key else f"sid:{session_id}"
        self._session_lock_cache[session_id] = key
        return key

    @staticmethod
    def _normalize_session_key(session_key: str) -> str:
        try:
            return f"key:{parse_session_key(session_key).to_key()}"
        except Exception:
            return f"key:{session_key}"

    def _spawn_execution(self, request: TaskExecutionRequest, lock_key: Optional[str]) -> None:
        if lock_key is not None:
            self._inflight_sessions.add(lock_key)
            # Recorded BEFORE ``create_task`` so the one way this lock can leak — the
            # task never being created, hence ``_on_execution_done`` never attached —
            # still leaves an owner the sweep can trace back to a dead execution.
            self._session_lock_owners[lock_key] = request.id
        task = asyncio.create_task(self._execute_claimed_request(request))
        self._inflight_executions[request.id] = task
        self._live_claimed_run_ids = frozenset(self._inflight_executions)
        self._inflight_requests[request.id] = request
        task.add_done_callback(
            lambda finished, rid=request.id, key=lock_key: self._on_execution_done(rid, key, finished)
        )

    def _on_execution_done(
        self, request_id: str, lock_key: Optional[str], task: "asyncio.Task[Any]"
    ) -> None:
        self._inflight_executions.pop(request_id, None)
        self._live_claimed_run_ids = frozenset(self._inflight_executions)
        request = self._inflight_requests.pop(request_id, None)
        interruption = self._inflight_cancellation_causes.pop(
            request_id,
            SETTLED_BY_INTERRUPTED,
        )
        if lock_key is not None:
            self._inflight_sessions.discard(lock_key)
            # Only if it is still OURS: a later execution may already have taken the
            # same key, and stealing its owner entry would make the sweep read that
            # live lock as leaked.
            if self._session_lock_owners.get(lock_key) == request_id:
                self._session_lock_owners.pop(lock_key, None)
        self._wake_runtime_work(
            RuntimeWorkLane.REQUESTS,
            RuntimeWorkLane.RUN_CALLBACKS,
            RuntimeWorkLane.FAILURE_NOTICES,
        )
        # ``_execute_claimed_request`` already terminalizes cancellations and
        # records ordinary failures. A task cancelled before its coroutine enters
        # has no execution to settle; put that bare claim back in the queue.
        if task.cancelled():
            if request is not None:
                try:
                    current = self.request_store.get_run(request.id)
                    if not current or current.get("status") in TERMINAL_RUN_STATUSES:
                        return
                    if (
                        current.get("pid") is None
                    ):
                        self.request_store.requeue(request.id)
                        return
                    written_status = self.request_store.complete(
                        request,
                        ok=False,
                        error=self._t(SETTLEMENT_I18N_KEYS[interruption]),
                        interrupt_reason=interruption,
                    )
                    if written_status is not None and request.task_id and request.request_type in {
                        "task_run",
                        "scheduled",
                    }:
                        self._project_terminal_definition_result(
                            self.request_store.get_run(request.id),
                            execution_id=request.id,
                            expected_status=written_status,
                        )
                        self.reconcile_jobs()
                except Exception:
                    logger.exception(
                        "Failed to terminalize canceled claimed request %s",
                        request_id,
                    )
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Claimed request %s crashed: %r", request_id, exc, exc_info=exc)

    def _claimed_request_backend_resolution(
        self,
        request: TaskExecutionRequest,
    ) -> _ClaimedRequestBackendResolution:
        observed = request.observed_activation_identity
        if observed is not None:
            return _ClaimedRequestBackendResolution(True, observed.backend)
        backend = str(request.agent_backend or "").strip()
        if backend:
            return _ClaimedRequestBackendResolution(True, backend)
        session_id = str(request.session_id or "").strip()
        if session_id:
            try:
                backend = str(resolve_session_id_target(session_id).agent_backend or "").strip()
            except ValueError:
                backend = ""
            if backend:
                return _ClaimedRequestBackendResolution(True, backend)

        agent_id = str(request.agent_id or "").strip()
        agent_name = str(request.agent_name or "").strip()
        if agent_id or agent_name:
            catalog = getattr(self.controller, "vibe_agent_store", None)
            get_by_id = getattr(catalog, "get_by_id", None)
            get_by_name = getattr(catalog, "get", None)
            service = getattr(self.controller, "agent_service", None)
            adapters = getattr(service, "agents", None)
            try:
                if agent_id:
                    if not callable(get_by_id):
                        if callable(
                            getattr(
                                service,
                                "runtime_activation_identity_for_request",
                                None,
                            )
                        ):
                            return _ClaimedRequestBackendResolution(False)
                        return _ClaimedRequestBackendResolution(True)
                    agent = get_by_id(agent_id)
                else:
                    if not callable(get_by_name):
                        if isinstance(adapters, dict) and agent_name in adapters:
                            return _ClaimedRequestBackendResolution(True, agent_name)
                        if callable(
                            getattr(
                                service,
                                "runtime_activation_identity_for_request",
                                None,
                            )
                        ):
                            return _ClaimedRequestBackendResolution(False)
                        return _ClaimedRequestBackendResolution(True)
                    agent = get_by_name(agent_name)
            except Exception:
                logger.exception(
                    "Failed to resolve durable Agent backend for Run %s",
                    request.id,
                )
                return _ClaimedRequestBackendResolution(False)
            if agent is None:
                # The execution owner still needs to claim and terminalize a Run
                # whose Agent was deleted. Authoritative absence means "no runtime
                # generation to probe", not "scan every adapter".
                return _ClaimedRequestBackendResolution(True)
            backend = str(getattr(agent, "backend", "") or "").strip()
            return _ClaimedRequestBackendResolution(bool(backend), backend)

        return _ClaimedRequestBackendResolution(True)

    def _activation_resolution_for_request(
        self,
        request: TaskExecutionRequest,
        *,
        backend: str = "",
    ) -> RuntimeActivationResolution | None:
        service = getattr(self.controller, "agent_service", None)
        identity_for_request = getattr(
            service,
            "runtime_activation_identity_for_request",
            None,
        )
        if not callable(identity_for_request):
            return None
        if not backend:
            return RuntimeActivationResolution(
                authoritative=True,
                identity=None,
            )

        request_view: Any = request
        session_id = str(request.session_id or "").strip()
        if session_id:
            try:
                target = resolve_session_id_target(session_id)
            except ValueError:
                target = None
            if target is not None and target.workdir:
                request_payload = vars(request).copy()
                request_payload["working_path"] = target.workdir
                request_view = SimpleNamespace(**request_payload)

        result = identity_for_request(backend, request_view)
        if isinstance(result, RuntimeActivationResolution):
            return result
        if isinstance(result, RuntimeActivationIdentity):
            return RuntimeActivationResolution(authoritative=True, identity=result)
        if result is None:
            return RuntimeActivationResolution(authoritative=True, identity=None)
        return RuntimeActivationResolution(authoritative=False)

    def _runtime_activation_registry(self) -> Any:
        service = getattr(self.controller, "agent_service", None)
        registry = getattr(service, "activation_registry", None)
        if registry is None:
            registry = getattr(self.controller, "runtime_activation", None)
        return registry

    def _claim_pending_request(
        self,
        pending: TaskExecutionRequest,
    ) -> TaskExecutionRequest | None:
        backend_resolution = self._claimed_request_backend_resolution(pending)
        if not backend_resolution.authoritative:
            return None
        backend = backend_resolution.backend
        service = getattr(self.controller, "agent_service", None)
        is_backend_ready = getattr(service, "is_backend_ready", None)
        if backend and callable(is_backend_ready) and not is_backend_ready(backend):
            return None
        resolution = self._activation_resolution_for_request(
            pending,
            backend=backend,
        )
        if resolution is not None and not resolution.authoritative:
            return None
        identity = resolution.identity if resolution is not None else None
        registry = self._runtime_activation_registry()
        if registry is None or identity is None:
            return self.request_store.claim(pending.id)

        committed = registry.commit_if_current(
            identity,
            lambda: self.request_store.claim(pending.id),
        )
        request = committed.value if committed.admitted else None
        if request is not None:
            request.observed_activation_identity = identity
        return request

    async def _mark_execution_started_for_claimed_request(
        self,
        request: TaskExecutionRequest,
    ) -> tuple[TaskExecutionRequest, bool]:
        observed_identity = request.observed_activation_identity
        request = self.request_store.refresh_claimed_request(request)
        if request.observed_activation_identity is None:
            request.observed_activation_identity = observed_identity
        backend_resolution = self._claimed_request_backend_resolution(request)
        if not backend_resolution.authoritative:
            self.request_store.requeue(request.id)
            return request, False
        backend = backend_resolution.backend
        service = getattr(self.controller, "agent_service", None)
        activation_identity = request.observed_activation_identity
        if activation_identity is None:
            resolution = self._activation_resolution_for_request(
                request,
                backend=backend,
            )
            if resolution is not None and not resolution.authoritative:
                self.request_store.requeue(request.id)
                return request, False
            activation_identity = (
                resolution.identity if resolution is not None else None
            )
        if not backend and activation_identity is not None:
            backend = activation_identity.backend
        if backend and service is not None:
            is_backend_ready = getattr(service, "is_backend_ready", None)
            if callable(is_backend_ready) and not is_backend_ready(backend):
                self.request_store.requeue(request.id)
                return request, False

        def commit_start() -> bool:
            return self.request_store.mark_execution_started(request.id)

        activation_registry = self._runtime_activation_registry()
        if activation_registry is not None and activation_identity is not None:
            committed = activation_registry.commit_if_current(
                activation_identity,
                commit_start,
            )
            if not committed.admitted:
                self.request_store.requeue(request.id)
                return request, False
            return request, bool(committed.value)
        return request, bool(commit_start())

    async def _execute_claimed_request(self, request: TaskExecutionRequest) -> None:
        request, execution_started = (
            await self._mark_execution_started_for_claimed_request(request)
        )
        if not execution_started:
            # A terminal result or explicit cancellation won after claim but before
            # this coroutine entered. Do not dispatch work whose Run no longer owns
            # the execution boundary.
            return
        error: Optional[str] = None
        #: The structured CLASS of this run's failure, when the failure has one. Kept
        #: beside ``error`` rather than parsed back out of it: the text is a sentence
        #: for a human, this is the value the notice's lane and label are chosen from.
        interrupt_reason: Optional[str] = None
        failure_code: Optional[str] = None
        should_complete = True
        settled_out_of_band = False
        recover_queue_on_return = False
        reconcile_delivery_on_return = False
        project_terminal_definition_on_cancel = False
        task_id = request.task_id
        session_key = request.session_key
        session_id = request.session_id
        #: Command-fire outcome facts. They stay ``None`` for every other request type,
        #: which is what keeps those rows' columns untouched.
        exit_code: Optional[int] = None
        stdout: Optional[str] = None
        stderr: Optional[str] = None
        timed_out: Optional[bool] = None
        #: The already-durable escalation turn a failed ``--on-failure agent`` command
        #: fire queued, or ``None``. Recorded on THIS run's metadata by ``complete()``,
        #: which is what stops the same failure being reported twice.
        escalation_run_id: Optional[str] = None
        try:
            from storage.resource_access_service import (
                HARNESS_ACCESS_FORBIDDEN_CODE,
                metadata_allows_harness_runtime,
            )

            if (
                request.request_type not in {"task_run", "scheduled"}
                and not metadata_allows_harness_runtime(request.metadata)
            ):
                raise PermissionError(HARNESS_ACCESS_FORBIDDEN_CODE)
            if request.request_type in {"task_run", "scheduled"}:
                # A scheduled one-shot is retired in the same SQLite transaction
                # that created this Run. Only that transaction stamps the Run as
                # its owner; cron fires keep the invalidation-aware fast path.
                consumed_generation = task_schedule_generation(request.metadata)
                consumed_one_shot = consumed_generation is not None
                if consumed_one_shot:
                    self.store.load()
                else:
                    self.store.maybe_reload()
                task = self.store.get_task(request.task_id or "")
                if task is None:
                    raise ValueError(f"task '{request.task_id}' not found")
                task_id = task.id
                session_key = task.session_key
                session_id = task.session_id
                if consumed_generation is not None and (
                    task.schedule_type != "at"
                    or task.enabled
                    or task.run_at != consumed_generation["run_at"]
                    or task.timezone != consumed_generation["timezone"]
                    or task.retired_at != consumed_generation["retired_at"]
                    or task.retirement_reason != TASK_RETIREMENT_SCHEDULE_CONSUMED
                    or task.last_run_id != request.id
                ):
                    raise ValueError(self._t("harness.task.scheduleReplaced"))
                task_agent_id = (
                    request.agent_id
                    if task.agent_name and task.agent_name == request.agent_name
                    else None
                )
                result = await self._execute_task(
                    task,
                    execution_id=request.id,
                    disable_one_shot=request.source_kind == "scheduler",
                    agent_id=task_agent_id,
                    schedule_generation=consumed_generation,
                )
                error = result.error
                session_key = result.session_key
                session_id = result.session_id
                failure_code = result.failure_code
                should_complete = result.complete_on_return
                reconcile_delivery_on_return = result.reconcile_delivery_on_return
                exit_code = result.exit_code
                stdout = result.stdout
                stderr = result.stderr
                timed_out = result.timed_out
                escalation_run_id = result.escalation_run_id
            elif request.request_type in {
                "hook_send",
                "watch",
                "webhook",
                "task_escalation",
            }:
                if not request.prompt:
                    raise ValueError("hook request requires prompt")
                user_context = self._resource_user_context(request.metadata)
                if request.session_policy == "create_per_run":
                    session_id = self._reserve_runtime_session(
                        agent_name=request.agent_name,
                        agent_id=request.agent_id,
                        deliver_key=request.deliver_key,
                        metadata=request.metadata,
                        workdir=request.metadata.get("session_workdir") if isinstance(request.metadata, dict) else None,
                        user_context=user_context,
                    )
                    session_key = ""
                elif not (request.session_id or request.session_key):
                    raise ValueError("hook request requires session_id or session_key")
                dispatch_result = await self._execute_request(
                    session_key=session_key,
                    session_id=session_id,
                    post_to=request.post_to,
                    deliver_key=request.deliver_key,
                    prompt=request.prompt,
                    execution_id=request.id,
                    task_id=task_id,
                    # ``delivery_intent_for_trigger`` has a CLOSED vocabulary, and an
                    # escalation IS a hook send -- an out-of-band turn a definition
                    # queued -- so it takes the hook intent rather than a new one no
                    # mapping knows. Provenance is not lost: the run row still carries
                    # ``run_type="task_escalation"`` and the failed fire's
                    # ``parent_run_id``.
                    trigger_kind=(
                        "hook"
                        if request.request_type in {"hook_send", "task_escalation"}
                        else request.request_type
                    ),
                    agent_name=request.agent_name,
                    # A composed hook / watch / webhook / escalation prompt is the one
                    # case where the request itself knows which part a person wrote, so
                    # its metadata must reach ``_build_context``.
                    metadata=request.metadata if isinstance(request.metadata, dict) else None,
                    user_context=user_context,
                    _capture_dispatch_result=True,
                    **({"agent_id": request.agent_id} if request.agent_id else {}),
                )
                if isinstance(dispatch_result, TaskDispatchResult):
                    error = dispatch_result.error
                    should_complete = dispatch_result.complete_on_return
                else:
                    error = dispatch_result
            elif request.request_type == "agent_run":
                message = _agent_run_message_for_request(request)
                if not message.strip():
                    # Whitespace-only counts as absent. ``MessageHandler`` returns
                    # early for a blank prompt without dispatching an agent, so
                    # accepting one here produced a run that could never receive a
                    # terminal result. Fail it at the door instead.
                    raise ValueError("agent run requires message")
                if not (request.session_id or request.session_key):
                    raise ValueError("agent run currently requires session_id or a resolvable session target")
                result = await self._execute_agent_run(
                    session_key=request.session_key,
                    session_id=request.session_id,
                    post_to=request.post_to,
                    deliver_key=request.deliver_key,
                    message=message,
                    execution_id=request.id,
                    agent_name=request.agent_name,
                    **({"agent_id": request.agent_id} if request.agent_id else {}),
                    metadata={
                        **(request.metadata or {}),
                        "source_kind": request.source_kind,
                        "source_actor": request.source_actor,
                        "parent_run_id": request.parent_run_id,
                        "callback_session_id": request.callback_session_id,
                    },
                )
                error = result.error
                should_complete = result.complete_on_return
                failure_code = result.failure_code
                settled_out_of_band = result.settled_out_of_band
                recover_queue_on_return = result.recover_queue_on_return
                if result.requeue_on_return:
                    requeue_metadata: dict[str, Any] = {}
                    if result.delivery_outcome is not None:
                        requeue_metadata[
                            AGENT_RUN_DELIVERY_OUTCOME_METADATA_KEY
                        ] = result.delivery_outcome
                    self.request_store.requeue(request.id, metadata=requeue_metadata)
            else:
                raise ValueError(f"unknown task request type: {request.request_type}")
        except asyncio.CancelledError:
            interrupt_reason = self._execution_interruption(request.id)
            error = self._t(SETTLEMENT_I18N_KEYS[interrupt_reason])
            should_complete = True
            settled_out_of_band = False
            recover_queue_on_return = False
            project_terminal_definition_on_cancel = request.request_type in {
                "task_run",
                "scheduled",
            }
            raise
        except UnresolvableSessionTarget as exc:
            # THE RUN'S DELIVERY TARGET IS GONE, and that is a CLASS of failure rather
            # than one more error string. #1060's field evidence is the case: a watch
            # pinned to a session that ceased to exist failed three deliveries and
            # stopped, and the only recorded cause anywhere was ``last_exit_code = 75``
            # — the user's own ``--retry-exit-code``, i.e. the waiter's healthy
            # "nothing new yet" signal. The error TEXT was already right here (it names
            # the missing session id); what was missing was a structured class, so the
            # notice could say the failure is about the DESTINATION and a reader could
            # tell it apart from the work itself breaking.
            #
            # Placed at the top level, not nested around the hook branch, so a run type
            # added later inherits the classification instead of having to remember it.
            # It cannot over-reach: ``UnresolvableSessionTarget`` is a distinct type
            # raised only by ``resolve_session_id_target``, never by a transient fault.
            #
            # Which branches actually arrive here. ``watch`` / ``hook_send`` / ``webhook``
            # and ``agent_run`` do — none of them resolves the target before dispatch, so
            # this is their first and only handler. ``task_run`` does NOT: ``_execute_task``
            # catches the same type first, runs the binding recovery
            # (``_recover_pinned_session_binding``), and absorbs a failed rebind retry in
            # its own ``except``. That asymmetry is deliberate and predates this handler —
            # a task has a definition to rebind or pause, and its recovery already stamps
            # a ``binding_change`` notice of its own.
            #
            # ONLY ``reason == "missing"`` IS CLASSIFIED. ``archived`` is left
            # unclassified on purpose: an archived session's row still exists, and the
            # honest description of that failure is "the session is inert", not "it no
            # longer exists". Its NOTICE is still deliverable — rung (5) reroutes an
            # archived session to the workspace inbox rather than writing into a row
            # ``list_inbox_sessions`` hides (see ``_rung_five_session_id``) — so the
            # reader is not left with an unnamed failure they cannot see. A wrong class
            # is worse than no class — the label is the one line in the notice a reader
            # trusts about the shape of the failure — and
            # ``notice_failure_class_i18n_key`` already renders NO line rather than a
            # generic one when there is nothing to name. Same for ``unusable``.
            error = str(exc)
            failure_code = FAILURE_CODE_UNRESOLVABLE_TARGET
            if exc.reason == "missing":
                interrupt_reason = INTERRUPT_REASON_DELIVERY_TARGET_MISSING
            logger.error(
                "Task execution request %s cannot reach its delivery target: %s",
                request.id,
                exc,
                exc_info=True,
            )
            should_complete = True
            settled_out_of_band = False
        except Exception as exc:
            error = str(exc)
            logger.error("Task execution request %s failed: %s", request.id, exc, exc_info=True)
            should_complete = True
            settled_out_of_band = False
        finally:
            if should_complete:
                written_status = self.request_store.complete(
                    request,
                    ok=not error,
                    error=error,
                    task_id=task_id,
                    session_key=session_key,
                    session_id=session_id,
                    interrupt_reason=interrupt_reason,
                    failure_code=failure_code,
                    exit_code=exit_code,
                    stdout=stdout,
                    stderr=stderr,
                    timed_out=timed_out,
                    escalation_run_id=escalation_run_id,
                )
                # A STOP THAT NEVER BECAME A ``CancelledError``. ``cancel_run`` can
                # commit its flag after the work returned, so ``complete`` normalizes
                # this run to ``canceled`` while the coroutine ran to the end without
                # ever being interrupted -- and a command stamp refused by that same
                # flag then left the definition still reporting the fire BEFORE this
                # one. Read from the status ACTUALLY written rather than from
                # ``project_terminal_definition_on_cancel``, which says only that the
                # cancellation arrived the usual way: a service shutdown cancels the
                # same coroutine and settles ``interrupted``, which is a stop of the
                # SERVICE and not of this fire.
                fire_was_stopped = written_status == "canceled" and request.request_type in {
                    "task_run",
                    "scheduled",
                }
                if fire_was_stopped:
                    # BEFORE the projection, because the escalation is claimable the
                    # whole time this coroutine is still finishing up.
                    self._retract_escalation_of_stopped_fire(request.id)
                if written_status is not None and (
                    # The projection is the lane that settles a canceled fire.
                    project_terminal_definition_on_cancel
                    or fire_was_stopped
                ):
                    self._project_terminal_definition_result(
                        self.request_store.get_run(request.id),
                        execution_id=request.id,
                        expected_status=written_status,
                    )
                    self.reconcile_jobs()
            if reconcile_delivery_on_return and session_id:
                manager = getattr(self.controller, "session_turns", None)
                reconcile_delivery = getattr(
                    manager,
                    "reconcile_terminal_run_delivery",
                    None,
                )
                if callable(reconcile_delivery):
                    try:
                        await reconcile_delivery(request.id, session_id=session_id)
                    except Exception:
                        logger.exception(
                            "Failed to reconcile terminal task Delivery: run=%s session=%s",
                            request.id,
                            session_id,
                        )
            if should_complete or settled_out_of_band:
                # An out-of-band settlement still made this run terminal, so it owes
                # the same callback follow-through as ``complete()``.
                await self._drain_callbacks()
            if (
                request.request_type == "agent_run"
                and session_id
                and (
                    should_complete
                    or settled_out_of_band
                    or recover_queue_on_return
                )
                and interrupt_reason is None
            ):
                manager = getattr(self.controller, "session_turns", None)
                recover_queue = getattr(
                    manager,
                    "recover_persisted_agent_run_queue",
                    None,
                )
                if callable(recover_queue):
                    try:
                        await recover_queue(session_id)
                    except Exception:
                        logger.exception(
                            "Failed to recover persisted Agent Run queue for session=%s",
                            session_id,
                        )

    def _runtime_default_workdir(self) -> Optional[str]:
        """The configured directory Agent work runs in, when nothing closer answered.

        Last stop before the ``~/.avibe`` fallback, and the reason that fallback is now
        nearly unreachable: a definition can legitimately carry no ``cwd`` and no
        readable Session -- a per-run binding created before
        ``_command_definition_spawn_cwd`` recorded one, or a definition written straight
        through the API -- and the product state directory is the worst possible place to
        run a user's command. ``runtime.default_cwd`` is where this install's Agent turns
        already run, so it is the same answer the escalation Session would get.

        Validated here rather than trusted: an unset or deleted ``default_cwd`` must fall
        through to the fallback below, not fail the fire with ``workdirMissing``.
        """

        config = getattr(self.controller, "config", None)
        candidate = getattr(getattr(config, "runtime", None), "default_cwd", None)
        if not isinstance(candidate, str) or not candidate.strip():
            return None
        resolved = os.path.abspath(os.path.expanduser(candidate.strip()))
        return resolved if os.path.isdir(resolved) else None

    def _bound_session_workdir(self, task: ScheduledTask) -> Optional[str]:
        """Where a command with no stored ``cwd`` should run: its binding's directory.

        ``--cwd`` is REFUSED for a definition bound to an existing Session
        (``cwd_with_existing_session``), on the rule that the Session owns its working
        directory -- so an escalating command task legitimately stores ``cwd=None``. A
        message task loses nothing to that: the Agent turn starts in the Session's
        workdir. A command has no turn to inherit it from, so ``None`` fell through to
        the ``~/.avibe`` fallback and ``--shell './scripts/sync.sh'`` -- the form the
        docs use -- ran from the product state directory, with the one flag that could
        have said otherwise rejected by the Session rule.

        Read live rather than copied into the definition at creation time, so the
        command follows a Session whose workdir was later changed, and so a per-run
        binding that does not exist yet simply reads as "no answer" (``None``) instead
        of a stale one. Best effort by design: a missing row, a NULL workdir or a read
        failure all fall back rather than failing the fire, and the fallback is still
        validated by the ``isdir`` check below.
        """

        session_id = str(task.session_id or "").strip()
        if not session_id:
            return None
        try:
            from storage.sessions_service import SQLiteSessionsService

            service = SQLiteSessionsService(paths.get_sqlite_state_path())
            try:
                row = service.get_agent_session_by_id(session_id)
            finally:
                # One store per FIRE, so it must not outlive the read: each one
                # carries an engine and an invalidation probe, and a cron command
                # running every minute would otherwise accumulate a connection a
                # minute for the life of the service.
                service.close()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "Could not read the workdir of Session %s bound to task %s: %s",
                session_id,
                task.id,
                exc,
            )
            return None
        if not row:
            return None
        return str(row.get("workdir") or "").strip() or None

    async def _execute_command_task(
        self,
        task: ScheduledTask,
        *,
        execution_id: str,
        disable_one_shot: bool,
        schedule_generation: Optional[dict[str, str]] = None,
    ) -> TaskExecutionResult:
        """Run one command definition's fire and record its outcome.

        The command runner owns the process mechanics -- spawn, spec handshake,
        timeout, cancellation and tree teardown -- so this method owns only scheduled
        task policy: where to run, how long to allow, and how the outcome is written
        to the definition and the run row.

        Every ORDERLY stop reaches the child through the runner: a timeout kills the
        tree from inside, while a service stop and a ``vibe runs cancel`` both cancel
        the awaiting coroutine, which the runner turns into a tree teardown (see
        ``_propagate_requested_cancellations`` for how the cancel flag becomes that
        cancellation). A SIGKILLed or crashed service reaches it through none of them:
        no ``CancelledError`` is ever delivered, and the isolated supervisor keeps its
        command running -- so a later fire of the same cron would start a second
        backup or migration beside the orphan. ``on_spawn`` therefore persists the
        worker identity on the run for ``_reap_orphaned_command_workers`` to find on
        the next start, and the ``finally`` clears it the moment the child is reaped.

        That identity lives in the run's ``metadata``, NOT in its ``pid`` column: that
        column holds the SERVICE pid, written by ``mark_execution_started`` for the
        recovery gate, and must not be repurposed.
        """

        error: Optional[str] = None
        exit_code: Optional[int] = None
        stdout_value: Optional[str] = None
        stderr_value: Optional[str] = None
        #: Whether the SCHEDULER stopped this fire, as opposed to the command choosing
        #: its own exit status. Recorded separately because the two are indistinguishable
        #: from the code alone: the runner reports a timeout as 124, and ``timeout 5 ...``
        #: inside a shell command reports a real one as 124 too.
        timed_out = False

        # BEFORE anything can fail, and before the spawn: the enqueue predicted this
        # command from the definition as it stood then, and the executor re-read the
        # definition after claiming. This is the copy that will actually run.
        non_owner_retired_expectation = (
            self.store._read_state(task)
            if schedule_generation is None
            and task.schedule_type == "at"
            and task.retired_at is not None
            else None
        )
        self._record_executed_command(execution_id, task)

        spawn_cwd = (
            task.cwd
            or self._bound_session_workdir(task)
            or self._runtime_default_workdir()
        )
        if not spawn_cwd:
            stable_cwd = paths.get_vibe_remote_dir()
            stable_cwd.mkdir(parents=True, exist_ok=True)
            spawn_cwd = str(stable_cwd)

        if not os.path.isdir(spawn_cwd):
            # Do NOT spawn: ``create_subprocess_exec`` would raise a bare
            # ``NotADirectoryError``/``FileNotFoundError`` whose text does not name the
            # directory the user configured.
            error = self._t("harness.command.workdirMissing", cwd=spawn_cwd)
        else:
            effective_timeout = (
                task.timeout_seconds
                if task.timeout_seconds is not None
                else COMMAND_TASK_DEFAULT_TIMEOUT_SECONDS
            )
            try:
                result = await run_supervised_command(
                    command=task.command,
                    shell_command=task.shell_command,
                    cwd=spawn_cwd,
                    timeout_seconds=effective_timeout,
                    label=f"scheduled task {task.id}",
                    on_spawn=lambda pid, identity: self._require_command_worker_record(
                        execution_id, identity
                    ),
                    max_output_bytes=COMMAND_TASK_OUTPUT_CAP_BYTES,
                )
            except SupervisedCommandStartupError as exc:
                # Its own clause, BEFORE the broad one: the class subclasses
                # ``RuntimeError``, so a single ``except Exception`` would swallow it and
                # report the worker's startup failure as an ordinary command error.
                error = self._localize_worker_error(exc.detail) or self._t(
                    "harness.command.supervisorExitedDuringStartup"
                )
            except Exception as exc:
                error = str(exc)
                logger.error(
                    "Scheduled task %s command failed: %s", task.id, exc, exc_info=True
                )
            else:
                exit_code = result.exit_code
                # THE WORKER'S OWN FAILURES ARRIVE HERE, not only through
                # ``SupervisedCommandStartupError``: that is raised solely when the
                # stdin handshake breaks, so a worker that accepted the spec and THEN
                # could not spawn (an argv whose executable does not exist is the
                # ordinary case) simply exits 1 with its machine-readable error line on
                # stderr. Left undecoded, that raw JSON became the definition's
                # ``last_error``, the failure notice, and the Agent escalation prompt.
                worker_error = decode_watch_worker_error(result.stderr or "")
                stderr_value = self._localize_worker_error(result.stderr)
                stdout_value = result.stdout
                if result.timed_out:
                    # Both streams are kept: whatever the command printed before it
                    # hung is the only evidence of WHY it hung, and a timeout has no
                    # exit status that explains itself. The runner preserves the
                    # capped readers' buffers across the kill for exactly this.
                    timed_out = True
                    error = self._t(
                        "harness.command.timedOut",
                        seconds=_format_seconds(effective_timeout),
                    )
                elif worker_error is not None:
                    # THE SUPERVISOR'S EXIT STATUS IS NOT THE COMMAND'S. ``main()``
                    # returns 1 for both of its own failures, so a spawn the worker
                    # could not perform arrived here as ``exit_code == 1`` and was
                    # published as a fact about the user's command: "Exit code: 1" in
                    # the failure notice and the escalation prompt, ``last_exit_code = 1``
                    # on the definition, an ``exit_code`` in the run row that
                    # ``vibe runs show`` prints -- for a command that never started, and
                    # alongside "Command exited with status 1" contradicting the
                    # supervisor sentence in the same message. There IS no command exit
                    # status in this outcome, and ``None`` is how this lane spells that
                    # (the handshake failure above reports it the same way). The error
                    # line the worker wrote is the discriminator, decoded rather than
                    # sniffed: only the supervisor emits it, before any of the command's
                    # own output can reach the stream, and a command that forged it is
                    # already reporting itself as a supervisor failure through
                    # ``_localize_worker_error`` -- losing an exit code it lied about is
                    # the smaller half of that.
                    #
                    # The WATCH lane keeps its exit code here by design: a cycle's status
                    # drives ``retry_exit_codes`` and ``_CycleResult`` has no "no status"
                    # to spell, so leave that behaviour to the lane that owns it.
                    exit_code = None
                    error = stderr_value or self._t(
                        "harness.command.unknownSupervisorFailure"
                    )
                elif exit_code:
                    detail = _last_nonempty_line(stderr_value)
                    error = (
                        self._t(
                            "harness.command.exitedWithDetail",
                            code=exit_code,
                            detail=detail,
                        )
                        if detail
                        else self._t("harness.command.exited", code=exit_code)
                    )
            finally:
                # Reached on success, on failure, and on the ``CancelledError`` a
                # stop raises: by the time control is here the runner has reaped
                # the tree, so the stored identity now names a dead pid that the
                # OS is free to hand to something else.
                self._clear_command_worker(execution_id)

        # THE ESCALATION IS COMPOSED BEFORE THE STAMP AND QUEUED BY IT. A definition
        # carrying ``on_failure == "agent"`` owes the user ONE Agent turn per failed
        # fire, carrying the report -- and that turn is what the stamp AUTHORISES, so
        # the two must be one transaction (HFR-269, ``core/watches.py``: "TWO COMMITS
        # ARE NOT ONE DECISION"). Split in two, a teardown landing between them queues
        # an escalation under a definition that no longer authorises it, and an
        # exception between them disables a failed one-shot while LOSING the report.
        #
        # Never ``enqueue_definition_run``: that writer re-reads the definition and
        # RAISES on a disabled one, and a failed ``at`` task is disabled by the very
        # stamp that authorises this turn.
        escalation_request = None
        if error and task.on_failure == "agent":
            escalation_request = self.request_store.build_hook_send(
                session_key=task.session_key or "",
                session_id=task.session_id,
                prompt=self._escalation_prompt(
                    task,
                    run_id=execution_id,
                    error=error,
                    exit_code=exit_code,
                    stdout=stdout_value,
                    stderr=stderr_value,
                ),
                post_to=task.post_to,
                deliver_key=task.deliver_key,
                agent_name=task.agent_name,
                session_policy=task.session_policy,
                run_type="task_escalation",
                definition_id=task.id,
                source_kind="scheduler",
                parent_run_id=execution_id,
                metadata=dict(task.metadata or {}),
            )
        stamped = self.store.mark_task_result(
            task.id,
            error=error,
            disable_one_shot=disable_one_shot,
            exit_code=exit_code,
            # This IS the command fire's own result, so its ``exit_code`` -- including
            # ``None`` for a fire that never spawned -- is the whole truth about the
            # definition's last status.
            records_command_outcome=True,
            timed_out=timed_out,
            # THE BINDING THIS FIRE STARTED AGAINST, and only when a turn is being
            # authorised. ``mark_task_result`` reloads the mirror and derives its
            # compare-and-set expectation from the RELOADED row, so a ``/new`` reclaim
            # committed while the command ran becomes the expectation -- it matches, and
            # the stamp lands. Harmless while the stamp wrote only bookkeeping; not
            # harmless now that it queues an Agent turn, because the escalation above was
            # composed from the PRE-EXECUTION task and would be durably queued against
            # the session the reclaim just tore down, with the notice suppressed in its
            # favour. Refusing instead rolls both halves back and leaves the failure to
            # the owed-notice ladder.
            expected_binding=(
                (task.session_id, task.session_key, task.schedule_type)
                if escalation_request is not None
                else None
            ),
            # AND THAT THIS FIRE HAS NOT BEEN STOPPED, re-read server-side for the same
            # reason and in the same transaction. A ``vibe runs cancel`` committed while
            # the command was exiting settles the run ``canceled`` a moment later, and
            # without this the turn it authorises would already be durably queued: the
            # user stops a job and the job answers by starting an Agent.
            expected_uncanceled_run_id=(
                execution_id if escalation_request is not None else None
            ),
            queued_run=(
                self.request_store.queued_run_payload(escalation_request)
                if escalation_request is not None
                else None
            ),
            expected_schedule_generation=schedule_generation,
            expected_terminal_run_id=(
                execution_id if schedule_generation is not None else None
            ),
            expected_non_owner_retired_one_shot=non_owner_retired_expectation,
        )
        # ONLY when the stamp landed. A refusal rolled the escalation row back with it,
        # so claiming an escalation id here would suppress the failure notice in favour
        # of a turn that does not exist -- and the failure would be silent.
        escalation_run_id = (
            escalation_request.id if escalation_request is not None and stamped else None
        )
        if not stamped:
            # Same refusal as the message path (HFR-261/HFR-264): the definition was
            # reclaimed, repointed or removed while this fire was running, so nothing
            # was stored. A would-be success must not be reported as one.
            logger.warning(
                "Scheduled task %s produced a result the store refused to stamp; "
                "reporting the run as failed",
                task.id,
            )
            if not error:
                error = self._t(_TASK_RESULT_NOT_RECORDED_I18N_KEY)
        # An ``at`` command task just disabled itself; the APScheduler job must drop.
        self.reconcile_jobs()
        return TaskExecutionResult(
            error=error,
            session_key=task.session_key or "",
            session_id=task.session_id,
            failure_code=None,
            complete_on_return=True,
            exit_code=exit_code,
            stdout=stdout_value,
            stderr=stderr_value,
            timed_out=timed_out,
            escalation_run_id=escalation_run_id,
        )

    def _escalation_prompt(
        self,
        task: ScheduledTask,
        *,
        run_id: str,
        error: str,
        exit_code: Optional[int],
        stdout: Optional[str],
        stderr: Optional[str],
    ) -> str:
        """The Agent-facing failure report a ``--shell ... --on-failure agent`` fire sends.

        NOT i18n'd, deliberately, and the watch completion-hook prompts are the
        precedent: this text is read by an AGENT, not shown to a user. It is the input
        to a turn, so the reply it produces is what the user actually reads -- and that
        reply is written in whatever language the conversation is in. Localizing the
        input would only make the instructions the model follows vary by locale.

        The user's own stored message comes FIRST when there is one: it is the standing
        instruction ("open a ticket if the backup fails"), and the machine-generated
        report is the evidence it operates on.
        """

        lines: list[str] = []
        stored_message = (task.prompt or "").strip()
        if stored_message:
            lines.append(stored_message)
            lines.append("")
        label = (task.name or "").strip()
        lines.append(
            f"A scheduled command task failed: {label} ({task.id})"
            if label
            else f"A scheduled command task failed: {task.id}"
        )
        command_preview = self._notice_command_preview(task)
        if command_preview:
            lines.append(f"Command: {command_preview}")
        # ``error`` already names the outcome in the only form that covers every branch:
        # the exit status, the timeout, a missing working directory, or the supervisor's
        # own startup detail. The exit code line is added only when the fire actually
        # produced one -- a run that never spawned has none, and "Exit code: None" would
        # be a fabricated fact on the one message that has to be trusted.
        lines.append(f"Outcome: {error}")
        if exit_code is not None:
            lines.append(f"Exit code: {exit_code}")
        stdout_tail = _last_nonempty_line(stdout)
        if stdout_tail:
            lines.append(f"Last stdout line: {stdout_tail}")
        stderr_tail = _last_nonempty_line(stderr)
        if stderr_tail:
            lines.append(f"Last stderr line: {stderr_tail}")
        lines.append(f"Run id: {run_id}")
        lines.append(f"Inspect with: vibe runs show {run_id}")
        return "\n".join(lines)

    async def _execute_task(
        self,
        task: ScheduledTask,
        *,
        execution_id: str,
        disable_one_shot: bool,
        agent_id: Optional[str] = None,
        schedule_generation: Optional[dict[str, str]] = None,
    ) -> TaskExecutionResult:
        from storage.resource_access_service import (
            HARNESS_ACCESS_FORBIDDEN_CODE,
            metadata_allows_harness_runtime,
        )

        error: Optional[str] = None
        complete_on_return = True
        reconcile_delivery_on_return = False
        failure_code: Optional[str] = None
        session_id = task.session_id
        session_key = task.session_key
        if not metadata_allows_harness_runtime(task.metadata):
            error = HARNESS_ACCESS_FORBIDDEN_CODE
            if not self.store.suspend_task(task.id, error=error):
                logger.warning(
                    "Remote-origin scheduled task %s changed before it could be suspended",
                    task.id,
                )
            self.reconcile_jobs()
            return TaskExecutionResult(
                error=error,
                session_key=session_key,
                session_id=session_id,
                failure_code=HARNESS_ACCESS_FORBIDDEN_CODE,
            )
        user_context = self._resource_user_context(task.metadata)
        binding_change: Optional[SessionBindingChange] = None
        # HFR-276: an earlier fire of THIS definition may have reserved a replacement
        # session it could not give back. The id is recorded on the definition, so the
        # retry belongs here -- before the fire, so a cleanup that succeeds is off the
        # books whatever this run does, and outside the ``try`` because it reports
        # itself and must never be mistaken for the run's own failure.
        self._retry_orphaned_reservations(task)
        if task.has_command:
            # A command definition runs a subprocess instead of prompting an Agent, so
            # none of the session/binding machinery below applies to it. An
            # ``on_failure == "agent"`` definition queues its escalation turn from in
            # there, in the same transaction as its result stamp -- it does not dispatch
            # one from here.
            return await self._execute_command_task(
                task,
                execution_id=execution_id,
                disable_one_shot=disable_one_shot,
                schedule_generation=schedule_generation,
            )
        try:
            if task.session_policy == "create_per_run":
                session_id = self._reserve_runtime_session(
                    agent_name=task.agent_name,
                    agent_id=agent_id,
                    deliver_key=task.deliver_key,
                    metadata=task.metadata,
                    workdir=task.cwd,
                    user_context=user_context,
                )
                session_key = ""
            dispatch_result = await self._execute_request(
                session_key=session_key,
                session_id=session_id,
                post_to=task.post_to,
                deliver_key=task.deliver_key,
                prompt=task.prompt,
                execution_id=execution_id,
                task_id=task.id,
                trigger_kind="scheduled",
                agent_name=task.agent_name,
                metadata=task.metadata,
                user_context=user_context,
                _capture_dispatch_result=True,
                **({"agent_id": agent_id} if agent_id else {}),
            )
            if isinstance(dispatch_result, TaskDispatchResult):
                error = dispatch_result.error
                complete_on_return = dispatch_result.complete_on_return
            else:
                error = dispatch_result
        except UnresolvableSessionTarget as exc:
            # The pinned session no longer resolves. Left alone this definition
            # re-fires and re-fails on every schedule with nobody told, so classify
            # first: a ``create_once`` definition reserved its own session and may
            # re-reserve one, anything else is user-pinned and gets paused. Either
            # way the user is told exactly once.
            logger.error("Scheduled task %s has an unresolvable session binding: %s", task.id, exc)
            binding_change = self._recover_pinned_session_binding(task, exc)
            error = binding_change.detail
            failure_code = FAILURE_CODE_UNRESOLVABLE_TARGET
            if binding_change.action == "rebound" and binding_change.new_session_id:
                # The binding failure was repaired. Any error from the retried turn
                # is a new backend/dispatch outcome, not another member of the
                # unresolvable-target auto-pause streak.
                failure_code = None
                session_id = binding_change.new_session_id
                session_key = ""
                try:
                    dispatch_result = await self._execute_request(
                        session_key=session_key,
                        session_id=session_id,
                        post_to=task.post_to,
                        deliver_key=task.deliver_key,
                        prompt=task.prompt,
                        execution_id=execution_id,
                        task_id=task.id,
                        trigger_kind="scheduled",
                        agent_name=task.agent_name,
                        metadata=task.metadata,
                        user_context=user_context,
                        _capture_dispatch_result=True,
                        **(
                            {"agent_id": agent_id}
                            if agent_id and task.agent_name
                            else {}
                        ),
                    )
                    if isinstance(dispatch_result, TaskDispatchResult):
                        error = dispatch_result.error
                        complete_on_return = dispatch_result.complete_on_return
                    else:
                        error = dispatch_result
                except Exception as retry_exc:
                    error = str(retry_exc)
                    logger.error(
                        "Scheduled task %s failed after rebinding to %s: %s",
                        task.id,
                        session_id,
                        retry_exc,
                        exc_info=True,
                    )
        except Exception as exc:
            error = str(exc)
            logger.error("Scheduled task %s failed: %s", task.id, exc, exc_info=True)
        if not self.store.mark_task_result(
            task.id,
            error=error,
            disable_one_shot=disable_one_shot,
            expected_schedule_generation=schedule_generation,
            expected_terminal_run_id=(
                execution_id if schedule_generation is not None else None
            ),
        ):
            # The TERMINAL STAMP was refused (HFR-261): the definition was reclaimed,
            # repointed, soft-deleted or removed while this fire was running, so
            # ``last_run_at`` / ``last_error`` / the one-shot disable are NOT stored.
            # Returning a result whose ``error`` is ``None`` here made
            # ``_execute_claimed_request`` complete the run ``ok=True`` -- the database
            # refused the stale stamp and both the caller AND the run ledger reported
            # success, while an ``at`` task that was never disabled can fire again.
            # Carried on the EXISTING error channel: a non-empty ``error`` is already
            # what makes ``complete(ok=not error)`` record the run as failed and what
            # the CLI and the Harness detail pane show. A real failure keeps its own
            # message; only a would-be success gains one. If a durable Delivery owns
            # the run, the caller reconciles that exact owner after the failed run
            # write commits so the Turn cannot later replace it with success.
            logger.warning(
                "Scheduled task %s produced a result the store refused to stamp; "
                "reporting the run as failed",
                task.id,
            )
            if not error:
                error = self._t(_TASK_RESULT_NOT_RECORDED_I18N_KEY)
            reconcile_delivery_on_return = not complete_on_return
            complete_on_return = True
        if binding_change is not None:
            # ``execution_id`` IS the run row's id on every path that reaches here
            # (``_execute_claimed_request`` passes ``request.id``), and this runs
            # BEFORE ``complete()`` settles the row — so a notice stamped now rides
            # into the terminal write instead of racing it. ``run_error`` decides
            # whether the binding news may take the notice slot at all: a rebind whose
            # retry failed already owes an ordinary failure notice, and that one must
            # not be displaced.
            await self._emit_binding_change(
                binding_change, run_id=execution_id, run_error=error
            )
        self.reconcile_jobs()
        return TaskExecutionResult(
            error=error,
            session_key=session_key,
            session_id=session_id,
            failure_code=failure_code if error else None,
            complete_on_return=complete_on_return,
            reconcile_delivery_on_return=reconcile_delivery_on_return,
        )

    async def _execute_agent_run(
        self,
        *,
        session_key: Optional[str],
        post_to: Optional[str],
        deliver_key: Optional[str],
        message: str,
        execution_id: str,
        session_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        agent_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> AgentRunExecutionResult:
        """Execute one direct Agent Run and wait for the real terminal result.

        Direct ``vibe agent run`` records model one concrete Agent turn. Async
        backends (Codex/Claude) return from ``handle_scheduled_message`` once the
        prompt is submitted, while their actual result arrives later through
        ``emit_agent_message``. Use the shared dispatch sink so the run stays
        ``running`` until that terminal result is emitted.

        The run may only stay ``running`` when the sink was released BY that terminal
        result. Any other release (no agent was dispatched, an external stop, a
        refused concurrent turn) means no result will ever arrive, so this method
        settles the run itself — otherwise the row is a permanent zombie, and its
        callback and session status hang with it. See
        ``docs/plans/agent-run-zombie-settlement.md``.
        """
        from core.services.dispatch import SOURCE_SCHEDULED, dispatch_turn_with_outcome
        from core.session_turns import TurnSubmissionResult

        target_info = resolve_session_id_target(session_id) if session_id else None
        target = target_info.session_key if target_info else parse_session_key(session_key or "")
        delivery_target = self._resolve_delivery_target(
            session_target=target,
            post_to=post_to,
            deliver_key=deliver_key,
        )
        context = await self._build_context(
            target,
            delivery_target=delivery_target,
            execution_id=execution_id,
            trigger_kind="agent_run",
            session_id=session_id,
            agent_name=agent_name,
            agent_id=agent_id,
            target_info=target_info,
            metadata=metadata,
        )

        gate = getattr(self.controller, "session_turn_gate", None)
        raw_delivery_intent = str(
            (metadata or {}).get(AGENT_RUN_DELIVERY_INTENT_METADATA_KEY)
            or AGENT_RUN_DELIVERY_STEER
        ).strip().lower()
        delivery_intent = normalize_agent_run_delivery_intent(raw_delivery_intent)
        if session_id and gate is None:
            return AgentRunExecutionResult(
                error=self._t(SESSION_TURN_GATE_UNAVAILABLE_I18N_KEY),
                complete_on_return=True,
                failure_code=FAILURE_CODE_SESSION_TURN_GATE_UNAVAILABLE,
            )
        if session_id and gate is not None:
            if raw_delivery_intent == LEGACY_AGENT_RUN_DELIVERY_SEND_NOW:
                state = await gate.submit_scheduled(
                    session_id,
                    context,
                    message,
                    delivery_intent=raw_delivery_intent,
                )
            elif delivery_intent != AGENT_RUN_DELIVERY_STEER:
                state = await gate.submit_scheduled(
                    session_id,
                    context,
                    message,
                    delivery_intent=delivery_intent,
                )
            else:
                state = await gate.submit_scheduled(session_id, context, message)
            route = state.route if isinstance(state, TurnSubmissionResult) else state
            delivery_outcome = None
            if isinstance(state, TurnSubmissionResult):
                delivery_outcome = {
                    "intent": delivery_intent,
                    "status": state.delivery_status or state.route,
                    "target_was_busy": state.target_was_busy,
                }
            if isinstance(state, TurnSubmissionResult) and state.delivery_status == "canceled":
                self.request_store.settle_without_result(
                    execution_id,
                    terminal_status="canceled",
                    metadata={
                        AGENT_RUN_DELIVERY_OUTCOME_METADATA_KEY: delivery_outcome,
                    },
                )
                return AgentRunExecutionResult(
                    error=None,
                    complete_on_return=False,
                    settled_out_of_band=True,
                    delivery_outcome=delivery_outcome,
                )
            if route == "enqueued":
                return AgentRunExecutionResult(
                    error=None,
                    complete_on_return=False,
                    requeue_on_return=not (
                        isinstance(state, TurnSubmissionResult)
                        and state.delivery_owner_transferred
                    ),
                    recover_queue_on_return=bool(
                        isinstance(state, TurnSubmissionResult)
                        and state.delivery_status == "flush_failed"
                    ),
                    delivery_outcome=delivery_outcome,
                )
            if route == "duplicate":
                return AgentRunExecutionResult(
                    error=None,
                    complete_on_return=True,
                )
            return AgentRunExecutionResult(
                error=None,
                complete_on_return=False,
                delivery_outcome=delivery_outcome,
            )

        async def _noop_chunk(_envelope: dict) -> None:
            return None

        outcome = await dispatch_turn_with_outcome(
            self.controller,
            context,
            message,
            source=SOURCE_SCHEDULED,
            on_chunk=_noop_chunk,
        )
        if outcome.error:
            return AgentRunExecutionResult(error=str(outcome.error), complete_on_return=True)
        if outcome.settled_by is not None and outcome.settled_by not in SETTLEMENTS_WITHOUT_RESULT:
            # Somebody else owns this run's terminal state: either the backend emitted
            # its real terminal result (``terminal_result``), or a terminal output
            # closed the turn while deliberately keeping the run elsewhere
            # (``turn_only_result`` — the requeued Activity behind a Claude delivery
            # failure). Tested as membership rather than "anything but
            # ``terminal_result``" so a future release reason cannot be misread as a
            # zombie (Codex P1).
            return AgentRunExecutionResult(error=None, complete_on_return=False)
        settled_by = outcome.settled_by or SETTLED_BY_NO_TERMINAL_RESULT
        if outcome.settled_by is None:
            # Unreachable: this path always passes ``on_chunk``. Settle rather than
            # trust an unexplained release — a wrong ``failed`` is recoverable, a
            # zombie is not.
            logger.warning(
                "agent run %s dispatched without a turn sink; settling as %s",
                execution_id,
                settled_by,
            )
        error_text = self._t(
            SETTLEMENT_I18N_KEYS.get(settled_by, SETTLEMENT_I18N_KEYS[SETTLED_BY_NO_TERMINAL_RESULT])
        )
        supported, _settled = self._settle_agent_run_without_result(
            execution_id,
            settled_by=settled_by,
            error=error_text,
        )
        if not supported:
            # Legacy file store: no guarded writer exists, so fall back to the
            # ``finally`` completion path rather than leave the run open.
            return AgentRunExecutionResult(error=error_text, complete_on_return=True)
        return AgentRunExecutionResult(
            error=error_text,
            complete_on_return=False,
            settled_out_of_band=True,
        )

    def settle_agent_runs_without_result(
        self,
        execution_ids: Sequence[str],
        *,
        settled_by: str,
    ) -> None:
        """Settle runs whose turn ended without a terminal result, on the TURN lane.

        ``_execute_agent_run`` can only settle the runs it dispatched itself. An
        avibe-targeted run goes through ``session_turn_gate.submit_scheduled``, which
        returns while the turn is still running, so ``SessionTurnManager`` calls this
        when that turn ends without a result (Codex P1). Same guarded writer, same
        i18n text as the drain lane — the only difference is who noticed.

        A settled run's terminal callback is delivered by the drain, so mark it dirty
        rather than waiting up to a full tick.
        """

        settled_any = False
        error_text = self._t(
            SETTLEMENT_I18N_KEYS.get(settled_by, SETTLEMENT_I18N_KEYS[SETTLED_BY_NO_TERMINAL_RESULT])
        )
        for raw_execution_id in execution_ids:
            execution_id = str(raw_execution_id or "").strip()
            if not execution_id:
                continue
            _supported, settled = self._settle_agent_run_without_result(
                execution_id,
                settled_by=settled_by,
                error=error_text,
            )
            if settled is None:
                continue
            settled_any = True
            self._project_terminal_definition_result(
                self.request_store.get_run(execution_id),
                execution_id=execution_id,
                expected_status=settled,
            )
        if settled_any:
            self._wake_runtime_work(
                RuntimeWorkLane.REQUESTS,
                RuntimeWorkLane.RUN_CALLBACKS,
                RuntimeWorkLane.FAILURE_NOTICES,
            )

    def settle_agent_runs_from_terminal_turn(
        self,
        execution_ids: Sequence[str],
        *,
        turn_id: str,
        outcome: str,
        settled_by: str | None,
        evidence_kind: str | None,
        evidence: Mapping[str, Any],
    ) -> None:
        """Settle late accepted Runs from their immutable Turn snapshot."""

        if settled_by in SETTLEMENTS_WITHOUT_RESULT:
            normalized_settlement = str(settled_by)
            store = self.request_store.sqlite_backend
            if store is None:
                self.settle_agent_runs_without_result(
                    execution_ids,
                    settled_by=normalized_settlement,
                )
                return
            normalized_execution_ids = list(
                dict.fromkeys(
                    execution_id
                    for value in execution_ids
                    if (execution_id := str(value or "").strip())
                )
            )
            terminal_status = SETTLEMENT_TERMINAL_STATUS.get(
                normalized_settlement,
                "failed",
            )
            error_text = self._t(
                SETTLEMENT_I18N_KEYS.get(
                    normalized_settlement,
                    SETTLEMENT_I18N_KEYS[SETTLED_BY_NO_TERMINAL_RESULT],
                )
            )
            provenance: dict[str, Any] = {
                "turn_id": turn_id,
                "evidence_kind": evidence_kind,
                "settled_by": normalized_settlement,
                "interrupt_reason": normalized_settlement,
            }
            if terminal_status == "failed":
                provenance["turn_failure_notification"] = {
                    "failure_id": f"turn:{turn_id}",
                    "delivered": False,
                }
            results = store.record_turn_run_outputs(
                normalized_execution_ids,
                output_id=f"resultless:{normalized_settlement}",
                text="",
                provenance=provenance,
                terminal_status=terminal_status,
                error=error_text,
            )
            repaired_ids = (
                store.reconcile_resultless_turn_failure_notices(
                    normalized_execution_ids,
                    turn_id=turn_id,
                    settled_by=normalized_settlement,
                )
                if terminal_status == "failed"
                else []
            )
            transitioned = False
            for execution_id, result in results.items():
                if not result.get("terminal_transition"):
                    continue
                transitioned = True
                run = result.get("run")
                expected_status = str((run or {}).get("status") or terminal_status)
                self._project_terminal_definition_result(
                    run,
                    execution_id=execution_id,
                    expected_status=expected_status,
                )
            if transitioned or repaired_ids:
                self._wake_runtime_work(
                    RuntimeWorkLane.REQUESTS,
                    RuntimeWorkLane.RUN_CALLBACKS,
                    RuntimeWorkLane.FAILURE_NOTICES,
                )
            return
        if evidence.get("settles_run") is not True:
            return
        store = self.request_store.sqlite_backend
        if store is None:
            logger.warning(
                "cannot settle late accepted Agent Runs for Turn=%s without SQLite",
                turn_id,
            )
            return
        terminal_status = {
            "completed": "succeeded",
            "failed": "failed",
            "canceled": "canceled",
        }.get(outcome)
        if terminal_status is None:
            return
        result_text = str(evidence.get("result_text") or "")
        terminal_error = evidence.get("terminal_error")
        message_id = str(evidence.get("message_id") or "").strip() or None
        output_provenance = evidence.get("output_provenance")
        output_provenance = (
            dict(output_provenance) if isinstance(output_provenance, Mapping) else {}
        )
        output_id = str(output_provenance.get("output_id") or "").strip()
        if not output_id and output_provenance.get("sequence") is not None:
            output_id = f"sequence:{output_provenance['sequence']}"
        if not output_id:
            output_id = "terminal"
        registry = getattr(
            getattr(getattr(self, "controller", None), "agent_service", None),
            "activities",
            None,
        )
        has_blocker = getattr(registry, "has_blocking_run_activity", None)
        normalized_execution_ids = list(
            dict.fromkeys(
                execution_id
                for value in execution_ids
                if (execution_id := str(value or "").strip())
            )
        )
        deferred_run_ids = [
            execution_id
            for execution_id in normalized_execution_ids
            if callable(has_blocker) and has_blocker(execution_id)
        ]
        results = store.record_turn_run_outputs(
            normalized_execution_ids,
            output_id=output_id,
            text=result_text,
            message_id=message_id,
            provenance={
                **output_provenance,
                "turn_id": turn_id,
                "evidence_kind": evidence_kind,
                "settled_by": settled_by,
            },
            terminal_status=terminal_status,
            error=str(terminal_error) if terminal_error is not None else None,
            deferred_run_ids=deferred_run_ids,
        )
        settled_any = any(
            result.get("terminal_transition") or result.get("text_backfilled")
            for result in results.values()
        )
        if settled_any:
            self._wake_runtime_work(
                RuntimeWorkLane.REQUESTS,
                RuntimeWorkLane.RUN_CALLBACKS,
                RuntimeWorkLane.FAILURE_NOTICES,
            )

    def _retract_escalation_of_stopped_fire(self, execution_id: str) -> None:
        """Take back the Agent turn a fire queued, once the user stops that fire.

        A failed ``--on-failure agent`` fire commits its stamp and its escalation in ONE
        transaction, guarded on the fire not being cancelled (HFR-269). That guard can
        only see the cancels that exist WHEN IT COMMITS: ``cancel_run`` on a running fire
        writes a flag, and the flag can land after the stamp has already returned --
        ``settle_run_terminal`` then re-reads it and normalizes this fire to ``canceled``
        while the durable escalation sits queued and claimable. The user stops a job and
        the job answers by starting an Agent, which is the same defect the compare-and-set
        was written for, one lane further along.

        WHY RETRACTION AND NOT ONE BIGGER TRANSACTION. The enqueue has to commit with the
        STAMP -- that is what authorises the turn, and splitting them is what HFR-269
        closed -- while this settlement happens later, in another lane, after
        ``reconcile_jobs``. There is no single transaction that holds both without
        reopening the window it fixed. So the escalation is committed optimistically and
        withdrawn by whoever settles the fire against it.

        NEITHER REPORT IS OWED HERE, and that is deliberate. ``canceled`` is written only
        when the user asked for this fire to stop (``_cancel_aware_terminal_status``), and
        a stopped fire owes no failure notice -- the same rule SCT-027 relies on when a
        teardown cancels the fire itself. Re-arming the notice instead would alert the
        user about a job they had just stopped, which is this defect's mirror image. The
        failure is not lost either way: the stamp landed, so the definition still reports
        it.

        Found by LINKAGE rather than from the local ``escalation_run_id``: a
        ``CancelledError`` delivered anywhere after the stamp skips the assignment
        entirely, and the escalation is durable by then regardless.

        THIS IS THE SECOND OF TWO GUARDS, and it is the weaker one. Retraction can only
        take back a row that is still queued: ``cancel_run`` on an escalation the
        dispatch loop has already claimed writes a flag, and the turn lane does not
        watch that flag (see ``_propagate_requested_cancellations`` for why a turn's
        stop belongs to the lane that owns it). So the claim itself refuses an
        escalation whose fire is already cancelled
        (``_escalates_a_stopped_fire``), which is what actually makes the ordering
        between these two lanes irrelevant: whichever runs first, no Agent turn starts
        for a stopped fire. What remains is only an escalation claimed BEFORE the user's
        stop landed -- a turn that was legitimately in flight when the request arrived,
        which is the same thing that happens to any other out-of-band turn and is
        stopped from the turn lane, not from here.
        """

        try:
            escalation = self.request_store.find_escalation_run(parent_run_id=execution_id)
        except Exception:
            logger.exception(
                "Failed to look for the escalation queued by stopped Run %s", execution_id
            )
            return
        if escalation is None:
            return
        escalation_id = str(escalation.get("id") or "").strip()
        status = str(
            _normalize_requested_run_status(escalation.get("status"))
            or escalation.get("status")
            or ""
        ).strip()
        if not escalation_id or status in TERMINAL_RUN_STATUSES:
            # Already settled -- including by the claim door, which refuses an
            # escalation whose fire is cancelled and terminalizes it as ``canceled``
            # exactly as this would have.
            return
        try:
            retracted = self.request_store.cancel_run(escalation_id)
        except Exception:
            logger.exception(
                "Failed to retract escalation Run %s for stopped Run %s",
                escalation_id,
                execution_id,
            )
            return
        if retracted:
            logger.info(
                "Retracted escalation Run %s: its command fire %s was stopped",
                escalation_id,
                execution_id,
            )

    def _project_terminal_definition_result(
        self,
        run: Optional[dict[str, Any]],
        *,
        execution_id: str,
        expected_status: str,
    ) -> None:
        """Project only the exact terminal transition the caller actually won."""

        if run is None:
            return
        status = str(run.get("status") or "").strip()
        if status != expected_status or status not in TERMINAL_RUN_STATUSES:
            return
        definition_id = str(
            run.get("definition_id") or run.get("task_id") or ""
        ).strip()
        if not definition_id:
            return
        self.store.maybe_reload()
        task = self.store.get_task(definition_id)
        if task is None:
            return
        run_session_id = str(run.get("session_id") or "").strip()
        task_session_id = str(task.session_id or "").strip()
        run_session_key = str(
            run.get("legacy_session_key") or run.get("session_key") or ""
        ).strip()
        task_session_key = str(task.session_key or "").strip()
        if (
            run_session_id != task_session_id
            or run_session_key != task_session_key
        ):
            logger.warning(
                "Run %s no longer matches scheduled definition %s; skipping result projection",
                execution_id,
                definition_id,
            )
            return
        error = None if status == "succeeded" else str(run.get("error") or "").strip()
        if status != "succeeded" and not error:
            metadata = run.get("metadata")
            interruption = (
                str(metadata.get("interrupt_reason") or "").strip()
                if isinstance(metadata, dict)
                else ""
            )
            key = SETTLEMENT_I18N_KEYS.get(interruption)
            if key is not None:
                error = self._t(key)
        if status != "succeeded" and not error:
            logger.warning(
                "Terminal Run %s has no failure detail; skipping definition %s projection",
                execution_id,
                definition_id,
            )
            return
        metadata = run.get("metadata")
        schedule_generation = task_schedule_generation(metadata)
        legacy_consumed_marker = bool(
            isinstance(metadata, dict)
            and metadata.get(TASK_SCHEDULE_CONSUMED_METADATA_KEY)
            and schedule_generation is None
        )
        if legacy_consumed_marker:
            logger.warning(
                "Run %s has no exact one-shot generation; skipping definition %s projection",
                execution_id,
                definition_id,
            )
            return
        retire_one_shot = (
            str(run.get("source_kind") or "") == "scheduler"
            and (
                schedule_generation is not None
                or (
                    self.store.sqlite_backend is None
                    and task.schedule_type == "at"
                )
            )
        )
        # A COMMAND fire that ends here ended without reaching its own result stamp --
        # cancelled, or interrupted by shutdown -- so this projection IS that fire's
        # authoritative outcome, and it has no exit status to report. Saying so
        # (``records_command_outcome``) is what clears the previous fire's code;
        # without it the row read "exited 7" beside a run the user had just stopped.
        # Only for command runs: the generic lane must never blank the column on
        # behalf of a message task, which never had a code of its own.
        records_command_outcome = (
            command_snapshot_of_run(run) is not None or task.has_command
        )
        raw_exit_code = run.get("exit_code")
        try:
            recorded = self.store.mark_task_result(
                task.id,
                error=error,
                disable_one_shot=retire_one_shot,
                exit_code=int(raw_exit_code) if raw_exit_code is not None else None,
                records_command_outcome=records_command_outcome,
                result_status=status,
                expected_binding=(
                    task.session_id,
                    task.session_key,
                    task.schedule_type,
                ),
                expected_schedule_generation=schedule_generation,
                expected_terminal_run_id=(
                    execution_id if schedule_generation is not None else None
                ),
            )
        except Exception:
            logger.exception(
                "Failed to project terminal Run %s to definition %s",
                execution_id,
                definition_id,
            )
            return
        if not recorded:
            logger.warning(
                "Definition %s changed before terminal Run %s could be projected",
                definition_id,
                execution_id,
            )
            return
        if retire_one_shot:
            try:
                job_id = self._job_ids.pop(task.id, task.id)
                if self.scheduler.get_job(job_id) is not None:
                    self.scheduler.remove_job(job_id)
                self._one_shot_job_identities.pop(job_id, None)
                self._job_signatures.pop(task.id, None)
            except Exception:
                logger.exception(
                    "Failed to retire scheduler job for terminal definition %s",
                    definition_id,
                )

    def _settle_agent_run_without_result(
        self,
        execution_id: str,
        *,
        settled_by: str,
        error: str,
    ) -> tuple[bool, Optional[str]]:
        """Terminalize a run whose turn ended without a terminal result.

        Deliberately NOT routed through ``_execute_claimed_request``'s ``finally``:
        that path writes via ``update_run_status``, whose UPDATE has no status
        predicate and would clobber a row a concurrent ``vibe runs cancel`` already
        settled. ``settle_without_result`` is scoped to ``queued|running`` and maps a
        cancel-requested run to ``canceled`` inside its own transaction.

        The terminal status comes from ``SETTLEMENT_TERMINAL_STATUS``: an explicit
        user stop is ``canceled``, an infrastructure fault is ``failed``.

        Returns whether the guarded writer exists and the status written by this
        exact terminal compare-and-set. An already-terminal row returns a supported
        writer with no status, so callers cannot project a result they did not settle.
        """

        if not self.request_store.supports_guarded_settlement():
            return False, None
        settled = self.request_store.settle_without_result(
            execution_id,
            terminal_status=SETTLEMENT_TERMINAL_STATUS.get(settled_by, "failed"),
            error=error,
            metadata={"interrupt_reason": settled_by},
        )
        if settled is None:
            logger.info(
                "Agent run %s was already settled elsewhere (%s)",
                execution_id,
                settled_by,
            )
        else:
            logger.warning(
                "Agent run %s settled %s without a terminal result (%s)",
                execution_id,
                settled,
                settled_by,
            )
        return True, settled

    # --- pinned session binding recovery -------------------------------------
    #
    # One choke point: classify -> maybe rebind -> maybe pause -> notify once.
    # The pause predicate is task-scoped (only a task has a definition to pause)
    # but the notification is not, so a run type added later inherits the
    # behaviour by default rather than by remembering to wire it.

    def _recover_pinned_session_binding(
        self,
        task: ScheduledTask,
        exc: UnresolvableSessionTarget,
    ) -> SessionBindingChange:
        previous = str(exc.session_id or task.session_id or "") or None
        snapshot = None
        if isinstance(task.metadata, dict):
            candidate = task.metadata.get(SESSION_SETTINGS_SNAPSHOT_KEY)
            if isinstance(candidate, dict):
                snapshot = candidate

        scope_id = ""
        if isinstance(task.metadata, dict):
            scope_id = str(task.metadata.get("session_scope_id") or "").strip()

        # ``existing`` is user-pinned: re-pointing it would lose the continuity the
        # pin exists to guarantee, so it is never rebound. Only ``create_once``
        # reserved its own session and may reserve another; ``create_per_run``
        # never reaches here because it reserves before every fire.
        if task.session_policy == "create_once" and (scope_id or task.deliver_key):
            rebound = self._rebind_create_once_session(task, snapshot)
            if rebound is not None:
                new_session_id, settings_preserved = rebound
                if not settings_preserved:
                    # Record the choice as an explicit, durable state, not as an
                    # absent ``agent_name``: ``vibe task update`` re-resolves an
                    # omitted Agent and writes it back, so without the flag the
                    # next unrelated edit re-pins the Agent this recovery dropped.
                    task.metadata = {
                        **(task.metadata if isinstance(task.metadata, dict) else {}),
                        BINDING_FOLLOWS_SESSION_METADATA_KEY: True,
                    }
                if not settings_preserved and task.agent_name:
                    # The reset rebind could not use the definition's own Agent --
                    # that is WHY it reset. Leaving the pin in place makes the
                    # storage fix cosmetic: ``_build_context`` sends it as
                    # ``vibe_agent_name``, which ``MessageHandler`` prioritises
                    # OVER the session row's Agent, so every fire would dispatch
                    # under the Agent that was just found unusable while the row
                    # says otherwise. Dropping the pin hands authority back to the
                    # rebound session, which is where a ``create_once``
                    # definition's Agent identity belongs -- and persisting it
                    # below is what makes the choice survive to the NEXT fire, not
                    # just this retry.
                    logger.info(
                        "Task %s dropped its stale Agent pin %r during a reset rebind; "
                        "the rebound session's Agent now governs",
                        task.id,
                        task.agent_name,
                    )
                    task.agent_name = None
                if not self._persist_task_session_id(task, new_session_id):
                    # THE REBIND DID NOT LAND (HFR-268). ``new_session_id`` is left
                    # unset on purpose: it is what ``_execute_task`` reads to decide
                    # whether to retry the fire, so an unstored rebind cannot dispatch
                    # a turn. The action is its own transition rather than "rebound" so
                    # that the durable ``binding_recovery`` marker and the notice both
                    # describe what actually happened -- reporting a rebind here is the
                    # HFR-266 lie one layer up.
                    #
                    # AND THE REPLACEMENT IS GIVEN BACK (HFR-270). The reservation
                    # already COMMITTED -- a different store, a different transaction,
                    # and for a standalone placement a mkdir as well -- so a refusal
                    # that only declined to dispatch left a live, unreferenced
                    # background session and its workspace behind, one more per fire
                    # that loses this race, with nothing that will ever run, list or
                    # delete them.
                    #
                    # AND THE ANSWER TO "WAS IT?" IS CONSUMED (HFR-276). The release is
                    # deliberately never fatal, so a locked database or an I/O fault
                    # returns ``False`` with the reservation still live: reporting
                    # "reclaimed" there is HFR-266's lie one layer inside HFR-270's fix,
                    # and the id -- random, never written to the definition -- would be
                    # lost with the log line. The losing branch records it instead, so
                    # the leak is TRACKED and a later fire can finish the job.
                    release_reason = f"the rebind of harness definition {task.id} was refused"
                    prefix = (
                        f"not rebound: {exc}. The definition was reclaimed, re-pointed or "
                        "removed while a replacement session was being reserved, so the "
                        "rebind was not stored and this run did not execute."
                    )
                    if self._release_reserved_session(new_session_id, reason=release_reason):
                        return SessionBindingChange(
                            action="reclaimed",
                            task_id=task.id,
                            reason=exc.reason,
                            previous_session_id=previous,
                            detail=(
                                f"{prefix} The next run re-resolves from the stored definition."
                            ),
                        )
                    tracked = self._track_orphaned_reservation(
                        task.id, new_session_id, release_reason
                    )
                    if tracked == "recorded":
                        cleanup = (
                            f"The replacement session {new_session_id} could NOT be given "
                            "back and is recorded on this definition; the next run retries "
                            "the cleanup."
                        )
                    elif tracked == "stamped":
                        cleanup = (
                            f"The replacement session {new_session_id} could NOT be given "
                            "back, and the record of it could not be stored either; its own "
                            "row names this definition, so the next run recovers it from "
                            "that stamp."
                        )
                    else:
                        cleanup = (
                            f"The replacement session {new_session_id} could NOT be given "
                            "back, and this definition no longer exists, so no later run "
                            "will look for it; it is reported here and in the logs only."
                        )
                    return SessionBindingChange(
                        action="orphaned",
                        task_id=task.id,
                        reason=exc.reason,
                        previous_session_id=previous,
                        detail=f"{prefix} {cleanup}",
                        orphaned_session_id=new_session_id,
                        orphan_tracked=tracked != "untracked",
                    )
                if settings_preserved:
                    detail = (
                        f"rebound to a new agent session {new_session_id}; the previous session "
                        f"({previous}) was deleted. Its workdir/agent/model were preserved."
                    )
                else:
                    detail = (
                        f"rebound to a new agent session {new_session_id}; the previous session "
                        f"({previous}) was deleted and its settings could not be recovered, so "
                        "scope defaults were used."
                    )
                return SessionBindingChange(
                    action="rebound",
                    task_id=task.id,
                    reason=exc.reason,
                    previous_session_id=previous,
                    detail=detail,
                    new_session_id=new_session_id,
                    settings_preserved=settings_preserved,
                )

        prior_failures = self.request_store.consecutive_definition_failures_with_code(
            task.id,
            FAILURE_CODE_UNRESOLVABLE_TARGET,
            limit=UNRESOLVABLE_TARGET_AUTO_PAUSE_FAILURES - 1,
        )
        failure_number = prior_failures + 1
        if failure_number < UNRESOLVABLE_TARGET_AUTO_PAUSE_FAILURES:
            return SessionBindingChange(
                action="failing",
                task_id=task.id,
                reason=exc.reason,
                previous_session_id=previous,
                detail=self._t(
                    "harness.run.bindingUnresolvedRetry",
                    session=previous or "",
                    attempt=failure_number,
                    threshold=UNRESOLVABLE_TARGET_AUTO_PAUSE_FAILURES,
                    id=task.id,
                ),
            )

        self._pause_task(task)
        return SessionBindingChange(
            action="paused",
            task_id=task.id,
            reason=exc.reason,
            previous_session_id=previous,
            detail=self._t(
                "harness.run.bindingUnresolvedPaused",
                session=previous or "",
                id=task.id,
            ),
        )

    def _rebind_create_once_session(
        self,
        task: ScheduledTask,
        snapshot: Optional[dict[str, Any]],
    ) -> Optional[tuple[str, bool]]:
        """Reserve a replacement session, preserving the lost one's settings.

        Returns ``(session_id, settings_preserved)``, or ``None`` when no session
        could be reserved at all. The snapshot is written by the reclaim that ran
        when the old row was deleted; where it is absent — a definition orphaned
        before that landed — the rebind falls back to scope defaults and says so,
        so the user can tell a preserved rebind from a reset one.
        """

        # Local, like every other ``core.vibe_agents`` use in this module (the Agent
        # catalog pulls in migrations/importer, which must not run at import time).
        from core.vibe_agents import AgentUnavailableError

        attempts: list[tuple[dict[str, Any], bool]] = []
        if snapshot:
            attempts.append(
                (
                    {
                        "agent_name": snapshot.get("agent_name") or task.agent_name,
                        "workdir": snapshot.get("workdir") or task.cwd,
                        "model": snapshot.get("model"),
                        "reasoning_effort": snapshot.get("reasoning_effort"),
                    },
                    True,
                )
            )
        # The non-preserving attempt must NOT re-send the definition's own Agent.
        # For a ``create_once`` definition ``task.agent_name`` is the same name the
        # snapshot carries, so when that Agent has been deleted or disabled the
        # fallback repeats the identical ``require_enabled`` failure and the
        # definition is paused -- when degrading to scope defaults is the entire
        # reason this attempt exists, and is what its own notice claims it did.
        # ``None`` is unambiguous here: ``_reserve_runtime_session`` reads it as
        # "resolve the scope Agent, else the default Agent", which is precisely
        # the intent. Only ``model``/``reasoning_effort`` need a sentinel, because
        # for them ``None`` is also a value a snapshot can legitimately carry.
        attempts.append(({"agent_name": None, "workdir": task.cwd}, False))

        for overrides, preserved in attempts:
            try:
                session_id = self._reserve_runtime_session(
                    deliver_key=task.deliver_key,
                    metadata=task.metadata,
                    definition_id=task.id,
                    user_context=self._resource_user_context(task.metadata),
                    **overrides,
                )
            except AgentUnavailableError as exc:
                # THE ONLY CONDITION THIS FALLBACK IS FOR: a snapshot naming an Agent
                # the user has since deleted or disabled must degrade to scope
                # defaults, not to a permanent failure (HFR-243).
                #
                # NARROW ON PURPOSE (HFR-265). A bare ``except Exception`` here read
                # SQLite contention, a migration failure and a filesystem error as
                # "that Agent is gone": the retry then succeeded against scope
                # defaults, ``_persist_task_session_id`` wrote the reset route, and the
                # definition PERMANENTLY lost the Agent/model the snapshot was holding
                # for it -- a transient fault turned into data loss, with the notice
                # claiming the settings "could not be recovered". Same shape as a broad
                # ``OperationalError`` retry, and the reason the deleted/disabled case
                # now has a type of its own instead of being inferred from a failure.
                logger.warning(
                    "Rebind reservation for task %s cannot use Agent %r (%s, preserved=%s): %s",
                    task.id,
                    exc.agent_name,
                    exc.reason,
                    preserved,
                    exc,
                )
                continue
            if session_id:
                return session_id, preserved
        return None

    def _persist_task_session_id(self, task: ScheduledTask, session_id: str) -> bool:
        """Store the rebind. ``False`` means the guard refused it — do NOT act on it.

        HFR-268. This used to swallow the conflict and return ``None``, and the comment
        said "the rebind stands for THIS fire only". It does not. The refusal is the
        database saying the definition was reclaimed, repointed or SOFT-DELETED inside
        the window (``expect.deleted_at`` is ``None``, so a ``RECLAIM_DELETE`` refuses
        here too), and the caller went on to dispatch the prompt and post the reply
        into the freshly reserved session anyway: the same shape as HFR-267, except the
        guard WAS asked first and its answer was dropped rather than never requested.
        A ``/new`` that pauses a ``create_once`` task mid-fire would tell the user the
        task was paused and then deliver a turn for it.
        """

        try:
            self._write_task_session_id(task, session_id)
        except DefinitionWriteConflict as exc:
            logger.warning("Could not persist the rebind for task %s: %s", task.id, exc)
            return False
        return True

    def _write_task_session_id(self, task: ScheduledTask, session_id: str) -> None:
        self.store.update_task(
            task.id,
            name=task.name,
            session_key=task.session_key,
            session_id=session_id,
            prompt=task.prompt,
            schedule_type=task.schedule_type,
            agent_name=task.agent_name,
            session_policy=task.session_policy,
            post_to=task.post_to,
            deliver_key=task.deliver_key,
            cron=task.cron,
            run_at=task.run_at,
            timezone_name=task.timezone,
            cwd=task.cwd,
            update_cwd=False,
            metadata=task.metadata,
        )

    def _pause_task(self, task: ScheduledTask) -> None:
        try:
            self.store.set_enabled(task.id, False)
        except KeyError:
            logger.debug("Task %s vanished before it could be paused", task.id)
        except DefinitionWriteConflict:
            # A teardown got there first (``/new`` pauses, the archive dialog
            # soft-deletes). The definition is already off; re-writing the whole row
            # from this stale mirror would only restore what the teardown cleared.
            logger.debug("Task %s was already reclaimed before it could be paused", task.id)

    async def _emit_binding_change(
        self,
        change: SessionBindingChange,
        *,
        run_id: Optional[str] = None,
        run_error: Optional[str] = None,
    ) -> None:
        """Notify once per binding transition, never once per fire.

        A daily cron on a dead session would otherwise notify every day. The
        dedup key is the transition, so a rebind (whose previous session id
        differs each time) always notifies while a definition re-fired against the
        same dead session stays quiet.

        The dedup marker IS the guarantee, so the notification is conditional on it
        landing: ``record_binding_recovery`` is a guarded write (HFR-261) that
        refuses when the definition was reclaimed, repointed or removed inside the
        window, and ignoring that ``False`` delivered a notice with nothing durable
        behind it — "once per transition" for a marker that was never stored, i.e.
        once per fire, forever, describing a recovery the stored definition no
        longer reflects (HFR-266).
        """

        task = self.store.get_task(change.task_id)
        if task is None:
            return
        recorded = task.metadata.get(BINDING_RECOVERY_METADATA_KEY) if isinstance(task.metadata, dict) else None
        if isinstance(recorded, dict) and recorded.get("signature") == change.signature:
            return
        recovery_payload = {
            "signature": change.signature,
            "action": change.action,
            "reason": change.reason,
            "previous_session_id": change.previous_session_id,
            "new_session_id": change.new_session_id,
            "settings_preserved": change.settings_preserved,
            "at": _utc_now_iso(),
            # Only on the HFR-276 branch, so the recorded shape of every other
            # transition is unchanged.
            **(
                {
                    "orphaned_session_id": change.orphaned_session_id,
                    "orphan_tracked": change.orphan_tracked,
                }
                if change.orphaned_session_id
                else {}
            ),
        }
        binding_notice = None
        if (
            run_id
            and change.action == "rebound"
            and change.new_session_id
            and not run_error
            and self.store._sqlite is not None
        ):
            binding_notice = {
                "run_id": run_id,
                "task_id": change.task_id,
                "signature": change.signature,
                "action": change.action,
                "reason": change.reason,
                "previous_session_id": change.previous_session_id,
                "new_session_id": change.new_session_id,
                "settings_preserved": change.settings_preserved,
            }
        try:
            recorded_recovery = self.store.record_binding_recovery(
                change.task_id,
                recovery_payload,
                binding_notice=binding_notice,
            )
        except Exception:
            logger.exception(
                "Not notifying the binding %s for task %s: its recovery marker and "
                "notice could not be committed together",
                change.action,
                change.task_id,
            )
            return
        if not recorded_recovery:
            logger.warning(
                "Not notifying the binding %s for task %s: the recovery record was "
                "refused, so the definition was reclaimed, repointed or removed and "
                "this transition no longer describes it",
                change.action,
                change.task_id,
            )
            return
        await self._notify_binding_change(task, change)

    async def _notify_binding_change(self, task: ScheduledTask, change: SessionBindingChange) -> None:
        """Single delivery seam for a binding change.

        Deliberately the only place that decides how the user hears about this.

        The durable half was already written by the time we get here (``last_error``
        plus ``metadata.binding_recovery``), and the run row's owed notice — which the
        drain delivers through D5's ladder — carries the user-visible half. For
        ``paused`` / ``reclaimed`` / ``orphaned`` that notice comes from the terminal
        transition, because those fires end failed. For ``rebound`` the retry succeeds
        and no transition owes anything, so ``record_binding_recovery`` commits the
        marker and notice together before this log seam is reached.

        Either way this seam does not deliver anything itself: doing so would produce
        a SECOND message for one event, and it would be the un-retried one, since only
        the owed notice has a receipt/backoff/dead-letter protocol behind it.

        What it does own is the log line, which is the operator's view of a
        transition that is per-BINDING rather than per-fire.
        """

        logger.warning(
            "Harness definition %s binding %s (reason=%s previous=%s new=%s preserved=%s): %s",
            task.id,
            change.action,
            change.reason,
            change.previous_session_id,
            change.new_session_id,
            change.settings_preserved,
            change.detail,
        )

    def audit_definition_bindings(self) -> list[tuple[str, str]]:
        """Report enabled definitions whose pinned session no longer resolves.

        Startup integrity check: a broken binding is otherwise invisible until the
        next fire, which for a weekly cron is a week of silence.
        """

        broken: list[tuple[str, str]] = []
        for task in self.store.list_tasks():
            if not task.enabled or not task.session_id:
                continue
            try:
                resolve_session_id_target(task.session_id)
            except UnresolvableSessionTarget as exc:
                broken.append((task.id, str(exc)))
            except Exception:
                # Never let an integrity probe take down startup.
                logger.debug("Binding audit skipped task %s", task.id, exc_info=True)
        if broken:
            logger.warning(
                "%d harness definition(s) point at an unresolvable agent session: %s",
                len(broken),
                "; ".join(f"{task_id} ({reason})" for task_id, reason in broken),
            )
        return broken

    def _reserve_runtime_session(
        self,
        *,
        agent_name: Optional[str] = None,
        agent_id: Optional[str] = None,
        deliver_key: Optional[str],
        metadata: Optional[dict[str, Any]] = None,
        workdir: Optional[str] = None,
        model: Any = _UNSET,
        reasoning_effort: Any = _UNSET,
        definition_id: Optional[str] = None,
        user_context: Any = None,
    ) -> str:
        """Reserve a background session for a run.

        ``model`` / ``reasoning_effort`` override the resolved Agent's values. A
        ``create_once`` rebind passes the snapshot of the session it lost (D3):
        without them this re-resolves the CURRENT Agent and silently changes the
        task's settings, because ``run_definitions`` has no column for either.

        Both take ``_UNSET`` rather than ``None`` as "not supplied". An explicit
        ``None`` means the reclaimed session pinned nothing and must keep pinning
        nothing; treating that as "not supplied" hands it the Agent's current
        model, which is a settings change recorded as a settings-preserving
        recovery. Omitting them is unchanged: the Agent's values are used.

        ``agent_name=None`` (or omitted) means "resolve the scope Agent, else the
        default Agent" -- the deliberate reset the non-preserving rebind wants.

        ``definition_id`` stamps the reserved row with the reserving definition's id,
        inside the reservation's own transaction (HFR-276): if the reservation
        committed, the durable handle committed with it, so an orphan whose
        ``orphaned_reservations`` record write is later refused by the same fault that
        refused the release is still recoverable from the row itself. Passed ONLY by
        the ``create_once`` recovery rebind, whose fires serialize on the pinned
        session's lock -- a ``create_per_run`` reservation is legitimately unbound and
        unreferenced between its reserve and its dispatch, and fires of such a
        definition can overlap, so stamping one would put a live reservation inside
        the sweep's definition of an orphan.
        """
        scope_id = ""
        if isinstance(metadata, dict):
            scope_id = str(metadata.get("session_scope_id") or "").strip()
        if not scope_id and deliver_key:
            # Definitions created before session_scope_id stored placement only
            # in deliver_key. Preserve those recurring definitions while new
            # definitions continue to persist the explicit metadata field.
            try:
                scope_id = parse_scope_id(deliver_key).session_scope
            except ValueError:
                try:
                    scope_id = parse_session_key(deliver_key).session_scope
                except ValueError:
                    pass
        from config import paths as config_paths
        from core.vibe_agents import AgentUnavailableError, VibeAgentStore
        from storage.importer import ensure_sqlite_state, resolve_primary_platform_from_config
        from storage.sessions_service import SQLiteSessionsService

        target = parse_scope_id(scope_id) if scope_id else None
        ensure_sqlite_state(primary_platform=resolve_primary_platform_from_config(config_paths.get_state_dir()))
        agent_store = VibeAgentStore()
        try:
            scope_target = (
                self._resolve_scope_agent_target(scope_id)
                if scope_id and not agent_name and not agent_id
                else _ScopeAgentTarget(None)
            )
            resolved_agent_name = agent_name or scope_target.agent_name
            if agent_id:
                agent = agent_store.require_reference_by_id(agent_id)
            elif resolved_agent_name:
                agent = agent_store.require_reference(resolved_agent_name)
            else:
                agent = agent_store.get_default_agent()
            if agent is not None and user_context is not None:
                agent = agent_store.require_accessible(
                    agent.name,
                    user_context=user_context,
                    enabled_only=True,
                )
        finally:
            agent_store.close()
        if agent is None:
            # Also the deleted/disabled-Agent condition, one step further out: the
            # default Agent itself is absent or off. Typed for the same reason
            # ``require_enabled`` is -- it is a settled catalog fact, not a fault --
            # so the non-preserving rebind attempt keeps degrading to a pause instead
            # of raising past its narrow catch. Message unchanged.
            raise AgentUnavailableError(
                "no enabled default Agent is available for session creation",
                agent_name=str(resolved_agent_name or ""),
                reason="no_default",
            )
        agent_backend = agent.backend
        # Which settings this session pins EXPLICITLY. Storing the value is not
        # enough: a preserved rebind writes NULL, and NULL already means "inherit
        # from the Agent at dispatch time" for every session in the table. Without
        # this marker the rebound session re-inherits at the next fire and D3 is
        # kept only until the Agent is next edited -- the storage layer preserves
        # it and the dispatch layer throws it away.
        from storage.session_reclaim import reconcile_explicit_overrides

        explicit_overrides = [
            key
            for key, value in (("model", model), ("reasoning_effort", reasoning_effort))
            if value is not _UNSET
        ]
        # Through the shared reconciler so this writer and every other writer of
        # these columns agree on the marker's shape.
        session_metadata = (
            reconcile_explicit_overrides(None, explicit=explicit_overrides)
            if explicit_overrides
            else None
        )
        if definition_id:
            from storage.sessions_service import RESERVED_BY_DEFINITION_METADATA_KEY

            session_metadata = {
                **(session_metadata or {}),
                RESERVED_BY_DEFINITION_METADATA_KEY: str(definition_id),
            }
        service = SQLiteSessionsService(config_paths.get_sqlite_state_path())
        try:
            common = {
                "metadata": session_metadata,
                "agent_backend": agent_backend,
                "session_anchor": (
                    f"{session_anchor_for_target(target)}:runtime_{uuid4().hex[:12]}"
                    if target is not None
                    else f"runtime_{uuid4().hex[:12]}"
                ),
                "agent_id": agent.id if agent else None,
                "agent_name": agent.name if agent else None,
                "model": (agent.model if agent else None) if model is _UNSET else model,
                "reasoning_effort": (
                    (agent.reasoning_effort if agent else None)
                    if reasoning_effort is _UNSET
                    else reasoning_effort
                ),
                "workdir": workdir,
                "visibility": "background",
            }
            if target is None:
                session_id = service.reserve_standalone_agent_session(**common)
            else:
                session_id = service.reserve_agent_session(
                    scope_key=target.session_scope,
                    **common,
                )
        finally:
            service.close()
        if not session_id:
            raise ValueError("failed to reserve runtime session")
        return session_id

    def _release_reserved_session(self, session_id: str, *, reason: str) -> bool:
        """Give back a session ``_reserve_runtime_session`` handed out and nothing adopted.

        WHY A RECLAIM AND NOT ONE ATOMIC OPERATION. The reservation and the write that
        adopts it are two different stores with two different engines
        (``SQLiteSessionsService`` and the definition store), and the standalone
        reservation also mkdirs a workspace -- a side effect no SQL transaction can roll
        back. Making them one operation would mean threading a single connection through
        both stores and still leaving the directory behind on a rollback. Naming the one
        row this call created and giving it back is smaller, is exact about WHICH row it
        may touch, and its safety does not depend on two stores continuing to live in the
        same database.

        Never fatal: this runs on a path that is already reporting a failure to the user,
        and a leaked reservation must not turn into a second exception on top of it.
        """

        from config import paths as config_paths
        from storage.sessions_service import SQLiteSessionsService

        service = SQLiteSessionsService(config_paths.get_sqlite_state_path())
        try:
            return service.release_reserved_agent_session(session_id, reason=reason)
        except Exception:
            logger.exception(
                "Could not release the reserved agent session %s (%s); it is now orphaned",
                session_id,
                reason,
            )
            return False
        finally:
            service.close()

    def _classify_reserved_session(self, session_id: str) -> str:
        """Which retry fact holds for a recorded reservation (HFR-279).

        The absence-only predecessor of this probe kept an ADOPTED row on the retry
        record forever: ``release_reserved_agent_session`` answers ``False`` for it,
        the row is not absent, so every later fire of the original definition took
        SQLite's write lock again to retry a cleanup that can never succeed. The
        classification makes the three ``False`` facts explicit -- ``absent`` and
        ``adopted`` entries are resolved and dropped, only a genuine ``reserved``
        orphan is worth a release attempt -- and it runs BEFORE the release, so an
        adopted winner never pays the release's ``BEGIN IMMEDIATE`` for the loser's
        bookkeeping. ``unknown`` is the fault verdict: a probe that could not read
        must keep the entry, never guess.
        """

        from config import paths as config_paths
        from storage.sessions_service import SQLiteSessionsService

        service = SQLiteSessionsService(config_paths.get_sqlite_state_path())
        try:
            return service.classify_reserved_agent_session(str(session_id))
        except Exception:
            logger.exception("Could not classify reserved agent session %s", session_id)
            return "unknown"
        finally:
            service.close()

    def _list_stamped_reservations(self, task_id: str) -> list[str]:
        """The reservations whose only surviving record is the stamp on their own row.

        HFR-276, the durable half: ``metadata.orphaned_reservations`` is written after
        a release fails, through the same database, so the fault that refused the
        release can refuse the record too. The reservation row itself committed before
        the fault -- that is what makes it an orphan -- and it carries the reserving
        definition's id in its metadata, stamped inside the same INSERT transaction.
        This listing recovers those ids. Never raises: the sweep is a cleanup and a
        fire must not fail because a cleanup could not even be enumerated.
        """

        from config import paths as config_paths
        from storage.sessions_service import SQLiteSessionsService

        service = SQLiteSessionsService(config_paths.get_sqlite_state_path())
        try:
            return service.list_reserved_agent_sessions_for_definition(str(task_id))
        except Exception:
            logger.exception(
                "Could not list stamped reserved sessions for harness definition %s", task_id
            )
            return []
        finally:
            service.close()

    def _track_orphaned_reservation(self, task_id: str, session_id: str, reason: str) -> str:
        """Record a reservation that could not be released. Answers HOW a later fire
        finds it: ``"recorded"``, ``"stamped"``, or ``"untracked"``.

        HFR-276. Appends rather than replaces: a definition that keeps losing the same
        race leaks one row per fire, and each of them needs its own id kept. Idempotent
        on the id so a retry that fails again does not grow the list.

        The record is the FAST half of the tracking, not the only half. It is written
        through the same database whose fault may just have refused the release, so
        the stated production cases -- a locked database, an I/O fault -- can refuse
        this write too. The reservation's own row already carries the reserving
        definition's id, stamped inside the reservation's transaction, so a refused
        record no longer loses the id: as long as the definition exists, its next fire
        sweeps the stamped rows (``_retry_orphaned_reservations``) and recovers it from
        the row itself -- that is ``"stamped"``. ``"untracked"`` is only the case with
        no next fire: the definition itself is gone (or its mirror was lost to the same
        fault, in which case the HFR-277 reload restores it and the sweep still runs --
        this answer under-claims rather than over-claims).
        """

        entries = self.store.list_orphaned_reservations(task_id)
        if any(str(entry.get("session_id") or "") == str(session_id) for entry in entries):
            return "recorded"
        entries.append({"session_id": str(session_id), "reason": reason, "at": _utc_now_iso()})
        # ``record_orphaned_reservations`` -> ``_write_task`` RAISES on a faulted write
        # (HFR-272) after reloading the mirror; only a guard REFUSAL comes back as
        # ``False``. This caller sits on a path that is already reporting a failure to
        # the user, and the stated production fault -- a locked database -- is exactly
        # the one that raises here, so both answers must land in the same "the record
        # did not happen" branch instead of the exception unwinding past the notice.
        try:
            recorded = self.store.record_orphaned_reservations(task_id, entries)
        except Exception:
            logger.exception(
                "Could not store the orphan record for reserved agent session %s on "
                "harness definition %s",
                session_id,
                task_id,
            )
            recorded = False
        if recorded:
            logger.warning(
                "Recorded reserved agent session %s as orphaned on harness definition %s (%s); "
                "the next run retries the release",
                session_id,
                task_id,
                reason,
            )
            return "recorded"
        if self.store.get_task(task_id) is not None:
            logger.warning(
                "Reserved agent session %s could not be released, and the orphan record on "
                "harness definition %s could not be stored either (%s); the row itself is "
                "stamped with the definition id, so the next run recovers it from that stamp",
                session_id,
                task_id,
                reason,
            )
            return "stamped"
        logger.error(
            "Reserved agent session %s could not be released OR recorded against harness "
            "definition %s (%s), and that definition no longer exists, so no later run will "
            "look for it; it is a live, unreferenced session and its row's definition stamp "
            "and this log line are the only records of its id",
            session_id,
            task_id,
            reason,
        )
        return "untracked"

    def _retry_orphaned_reservations(self, task: ScheduledTask) -> None:
        """Finish a release that failed on an earlier fire of this definition.

        The retry is bounded and self-limiting on purpose: it runs only once per fire
        and only against ids this definition's own recovery reserved -- the ones on the
        ``orphaned_reservations`` record, plus the ones whose record write itself was
        refused and whose only surviving handle is the definition-id stamp on their own
        row (HFR-276). Entries are dropped when the row is gone or was adopted after
        all, released when it is still a reservation, and kept when the release failed
        again or the classification could not read, so a database that is down keeps
        the fact rather than losing it. Never raises: a fire must not fail because a
        cleanup did.

        CLASSIFY, THEN RELEASE (HFR-279). The release's own ``False`` conflates "gone",
        "adopted" and "faulted", and the absence-only probe that used to disambiguate
        it resolved only the first: an adopted reservation stayed on the record forever
        and every fire re-took SQLite's write lock to retry a cleanup that can never
        succeed. The classification runs first, without the write lock, so an adopted
        winner's row is never even read under ``BEGIN IMMEDIATE`` again -- it is
        resolved off the record WITHOUT being touched. Only a row that is still
        empty-native and unreferenced is worth the guarded release, whose predicates
        re-assert both facts under the write lock, so a classification that raced an
        adoption destroys nothing: the release backs off, the entry stays, and the next
        fire classifies it as adopted and drops it.
        """

        from storage.sessions_service import (
            RESERVATION_ABSENT,
            RESERVATION_ADOPTED,
            RESERVATION_RESERVED,
        )

        entries = task.metadata.get(ORPHANED_RESERVATIONS_METADATA_KEY) if isinstance(task.metadata, dict) else None
        if not isinstance(entries, list):
            entries = []
        recorded_ids = {
            str(entry.get("session_id") or "").strip()
            for entry in entries
            if isinstance(entry, dict)
        }
        # The durable backstop: reservations this definition stamped but whose record
        # write was refused by the same fault that refused the release. Recovered from
        # the rows themselves, appended after the recorded entries so a recorded id is
        # handled exactly once.
        stamped = [
            {"session_id": session_id}
            for session_id in self._list_stamped_reservations(task.id)
            if session_id not in recorded_ids
        ]
        if not entries and not stamped:
            return
        remaining: list[dict[str, Any]] = []
        released: list[str] = []
        for entry in [*entries, *stamped]:
            if not isinstance(entry, dict):
                continue
            session_id = str(entry.get("session_id") or "").strip()
            if not session_id:
                continue
            reason = str(entry.get("reason") or "") or f"the rebind of harness definition {task.id} was refused"
            state = self._classify_reserved_session(session_id)
            if state in (RESERVATION_ABSENT, RESERVATION_ADOPTED):
                if state == RESERVATION_ADOPTED:
                    logger.info(
                        "Reserved agent session %s recorded on harness definition %s was "
                        "adopted and is somebody's live binding; dropping it from the "
                        "retry record without touching it",
                        session_id,
                        task.id,
                    )
                released.append(session_id)
            elif state == RESERVATION_RESERVED and self._release_reserved_session(
                session_id, reason=reason
            ):
                released.append(session_id)
            else:
                # Still a reservation but the release failed, or the classification
                # could not read: both keep the entry. A stamped id that stays here is
                # promoted onto the record, so the fact survives even if the row's
                # stamp is somehow lost later.
                remaining.append(dict(entry))
        if not released and not stamped:
            return
        # Same shape as ``_track_orphaned_reservation``: a faulted write RAISES out of
        # the store (HFR-272) and this method promises a fire never fails because a
        # cleanup did, so the raise and the refusal are one branch here.
        try:
            recorded = self.store.record_orphaned_reservations(task.id, remaining)
        except Exception:
            logger.exception(
                "Could not update the orphan record on harness definition %s", task.id
            )
            recorded = False
        # Keep the object THIS fire is acting from in step with what the sweep just
        # established, on BOTH branches: the store write may have reloaded the cache
        # out from under it (``record_orphaned_reservations`` starts with
        # ``maybe_reload``, and a raised write reloads too), and the very next thing a
        # ``create_once`` recovery does is hand ``task.metadata`` back to
        # ``update_task`` as a full-row payload. Left unreconciled on the failure
        # branch, that later write would durably RE-RECORD entries this very fire just
        # resolved. ``remaining`` is correct either way: it holds exactly the entries
        # still worth a retry, so a later full-row write that carries it repairs the
        # record the failed write left stale.
        self._forget_orphaned_reservations(task, remaining)
        if recorded:
            if released:
                logger.info(
                    "Resolved orphaned reservation(s) %s recorded on harness definition %s",
                    ", ".join(released),
                    task.id,
                )
            return
        # The cleanup landed but the bookkeeping did not, so the durable entries stay
        # and the next fire retries them: ``release_reserved_agent_session`` names one
        # row by id and an already-released id simply reports "gone" under the
        # classification, so a repeat is a no-op rather than a second delete. A stamped
        # entry that could not be promoted is equally safe: its stamp still names this
        # definition, so the next sweep finds it again from the row itself.
        logger.warning(
            "Resolved orphaned reservation(s) %s for harness definition %s, but the "
            "record could not be updated; the next run re-checks them",
            ", ".join(released) or "(none)",
            task.id,
        )

    @staticmethod
    def _forget_orphaned_reservations(task: ScheduledTask, remaining: list[dict[str, Any]]) -> None:
        metadata = dict(task.metadata) if isinstance(task.metadata, dict) else {}
        if remaining:
            metadata[ORPHANED_RESERVATIONS_METADATA_KEY] = [dict(entry) for entry in remaining]
        else:
            metadata.pop(ORPHANED_RESERVATIONS_METADATA_KEY, None)
        task.metadata = metadata

    def _resolve_scope_agent_target(self, deliver_key: str) -> "_ScopeAgentTarget":
        try:
            target = parse_scope_id(deliver_key)
        except ValueError:
            try:
                target = parse_session_key(deliver_key)
            except ValueError:
                return _ScopeAgentTarget(None)
        from config import paths as config_paths
        from storage.settings_service import make_scope_id

        scope_id = make_scope_id(target.platform, target.scope_type, target.scope_id)
        engine = create_sqlite_engine(config_paths.get_sqlite_state_path())
        try:
            with engine.connect() as conn:
                value = conn.execute(
                    select(scope_settings.c.agent_name)
                    .where(scope_settings.c.scope_id == scope_id)
                    .limit(1)
                ).first()
        finally:
            engine.dispose()
        if value is None:
            return _ScopeAgentTarget(None)
        agent_name = str(value.agent_name).strip() if value.agent_name else None
        return _ScopeAgentTarget(agent_name)

    async def _execute_request(
        self,
        *,
        session_key: Optional[str],
        post_to: Optional[str],
        deliver_key: Optional[str],
        prompt: str,
        execution_id: str,
        task_id: Optional[str] = None,
        trigger_kind: str,
        session_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        agent_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        user_context: Any = None,
        _capture_dispatch_result: bool = False,
    ) -> Optional[str] | TaskDispatchResult:
        target_info = resolve_session_id_target(session_id) if session_id else None
        self._require_execution_agent_access(
            agent_name=agent_name,
            target_info=target_info,
            user_context=user_context,
        )
        target = target_info.session_key if target_info else parse_session_key(session_key or "")
        delivery_target = self._resolve_delivery_target(
            session_target=target,
            post_to=post_to,
            deliver_key=deliver_key,
        )
        context = await self._build_context(
            target,
            delivery_target=delivery_target,
            execution_id=execution_id,
            task_id=task_id,
            trigger_kind=trigger_kind,
            session_id=session_id,
            agent_name=agent_name,
            agent_id=agent_id,
            target_info=target_info,
            # Forwarded like the ``agent_run`` path already does. Without it every
            # metadata field ``_build_context`` reads — the display-prompt override, the
            # source/parent provenance — is unreachable for an enqueued hook, watch,
            # webhook, or escalation request, which are exactly the composed kinds whose
            # echo depends on being handed the user-authored instruction.
            metadata=metadata,
        )
        # A scheduled avibe turn drives the sidebar dot through the SAME two
        # chokepoints as any other turn — inbound AgentService.handle_message
        # (running) and the outbound terminal result (idle/failed) — because its
        # ``context`` carries the avibe ``agent_session_id`` (set in
        # ``_build_context``). No dot bookkeeping here.
        #
        # Route every persisted Session target through the durable owner. The
        # source policy decides whether this input queues or steers; the platform
        # only controls native routing and outward delivery.
        # Returning ``None`` keeps ``ok = not error`` true (the run's own outcome
        # surfaces via the outbound terminal result + sidebar dot, exactly as the
        # interactive Chat turn does).
        gate = getattr(self.controller, "session_turn_gate", None)
        if session_id and gate is not None:
            from core.message_priority import delivery_intent_for_trigger
            from core.session_turns import TurnSubmissionResult

            submission = await gate.submit_scheduled(
                session_id,
                context,
                prompt,
                delivery_intent=delivery_intent_for_trigger(trigger_kind),
            )
            if isinstance(submission, TurnSubmissionResult):
                result = TaskDispatchResult(
                    error=None,
                    complete_on_return=not submission.delivery_owner_transferred,
                )
                return result if _capture_dispatch_result else result.error
            result = TaskDispatchResult(error=None)
            return result if _capture_dispatch_result else result.error
        error = await self.controller.message_handler.handle_scheduled_message(
            context=context,
            message=prompt,
            parsed_session_key=target,
        )
        result = TaskDispatchResult(error=error)
        return result if _capture_dispatch_result else result.error

    @staticmethod
    def _resource_user_context(metadata: Optional[dict[str, Any]]) -> Any:
        from storage.resource_access_service import resource_user_context_from_metadata

        return resource_user_context_from_metadata(metadata)

    @staticmethod
    def _require_execution_agent_access(
        *,
        agent_name: Optional[str],
        target_info: Optional[ResolvedSessionIdTarget],
        user_context: Any,
    ) -> None:
        if user_context is None:
            return

        from core.vibe_agents import VibeAgentAccessError, VibeAgentStore, ensure_agent_selection_access

        selected_name = str(agent_name or "").strip() or None
        selected_id = None
        if selected_name is None and target_info is not None:
            selected_name = str(target_info.agent_name or "").strip() or None
            selected_id = str(target_info.agent_id or "").strip() or None
        if selected_name is None and selected_id is None:
            raise VibeAgentAccessError("Agent access is not permitted.")

        store = VibeAgentStore()
        try:
            with store.engine.connect() as connection:
                ensure_agent_selection_access(
                    connection,
                    agent_name=selected_name,
                    agent_id=selected_id,
                    user_context=user_context,
                    missing_is_error=True,
                )
        finally:
            store.close()

    async def _build_context(
        self,
        target: ParsedSessionKey,
        *,
        delivery_target: Optional[ParsedSessionKey] = None,
        execution_id: str,
        task_id: Optional[str] = None,
        trigger_kind: str = "scheduled",
        session_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        agent_id: Optional[str] = None,
        target_info: Optional[ResolvedSessionIdTarget] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> MessageContext:
        platform = target.platform
        self.validate_platform(platform)
        delivery_target = delivery_target or target
        session_target_context = self._resolve_target_context(target)
        delivery_target_context = self._resolve_target_context(delivery_target)
        delivery_strategy = self._build_delivery_alias_strategy(
            session_target=target,
            delivery_target=delivery_target,
            session_context=session_target_context,
            delivery_context=delivery_target_context,
        )

        # avibe workbench: the context IDENTITY is the concrete session, not the
        # project scope — an avibe project holds many independent sessions, so
        # keying the context off the project id would make _get_session_key /
        # consolidated-log grouping collide between concurrent runs in the same
        # project (they'd edit/merge each other's log). Use session_id as the
        # channel_id (matches how the interactive Chat dispatch builds the context);
        # persistence/routing still resolves the project scope via agent_session_id.
        channel_id = session_target_context["channel_id"]
        if platform == "avibe" and session_id:
            channel_id = session_id
        from core.services.session_fork import fork_metadata_from_request, fork_metadata_from_session_metadata

        native_session_fork = fork_metadata_from_request(metadata)
        if native_session_fork is None and target_info and not str(target_info.native_session_id or "").strip():
            native_session_fork = fork_metadata_from_session_metadata(getattr(target_info, "metadata", None))

        definition_name, definition_prompt = self._definition_display_fields(task_id)

        return MessageContext(
            user_id=session_target_context["user_id"],
            channel_id=channel_id,
            platform=platform,
            thread_id=target.thread_id,
            message_id=self._build_message_id(
                execution_id=execution_id,
                task_id=task_id,
                trigger_kind=trigger_kind,
            ),
            platform_specific={
                "platform": platform,
                "is_dm": target.is_dm,
                "turn_source": "scheduled",
                "agent_session_id": session_id,
                "session_key_external": target.to_key(),
                "delivery_key_external": delivery_target.to_key(),
                "delivery_scope_session_key": delivery_target.session_scope,
                "delivery_override": {
                    "user_id": delivery_target_context["user_id"],
                    "channel_id": delivery_target_context["channel_id"],
                    "thread_id": delivery_target.thread_id,
                    "platform": platform,
                    "is_dm": delivery_target.is_dm,
                },
                "scheduled_delivery_alias": delivery_strategy,
                "task_execution_id": execution_id,
                "task_trigger_kind": trigger_kind,
                # Provenance source_id for harness-originated turns: the run
                # definition id (task / watch). Carried so the message mirror can
                # attribute the injected prompt to its precise definition.
                "task_definition_id": task_id,
                # Human label for the same definition, so an outward echo of the
                # injected prompt can name what fired instead of an opaque id.
                "task_definition_name": definition_name,
                # The definition's STORED instruction, i.e. the part of this run's
                # prompt a person actually wrote. A watch / hook / webhook prompt is
                # composed for the agent to read and appends machine-generated
                # evidence (waiter stdout, a failure report, a webhook payload); an
                # outward echo may show only this segment, never the composed text.
                "harness_display_prompt": (
                    str((metadata or {}).get("harness_display_prompt") or "").strip() or definition_prompt
                ),
                "vibe_agent_name": agent_name,
                "vibe_agent_id": agent_id,
                "source_kind": (metadata or {}).get("source_kind"),
                "source_actor": (metadata or {}).get("source_actor"),
                "source_session_id": (metadata or {}).get("source_session_id"),
                "vault_request_type": (metadata or {}).get("vault_request_type"),
                "vault_request_status": (metadata or {}).get("vault_request_status"),
                "parent_run_id": (metadata or {}).get("parent_run_id"),
                "callback_session_id": (metadata or {}).get("callback_session_id"),
                "suppress_delivery": bool(target_info.suppress_delivery) if target_info else False,
                "agent_session_target": (
                    {
                        "id": target_info.session_id,
                        "agent_id": target_info.agent_id,
                        "agent_name": target_info.agent_name,
                        "agent_backend": target_info.agent_backend,
                        "agent_variant": target_info.agent_variant,
                        "model": target_info.model,
                        "reasoning_effort": target_info.reasoning_effort,
                        "native_session_id": target_info.native_session_id,
                        "native_session_fork": native_session_fork,
                        "workdir": target_info.workdir,
                        "session_anchor": target_info.session_anchor,
                        "metadata": getattr(target_info, "metadata", None) or {},
                        "suppress_delivery": target_info.suppress_delivery,
                    }
                    if target_info
                    else None
                ),
            },
        )

    def _definition_display_fields(
        self, definition_id: Optional[str]
    ) -> tuple[Optional[str], Optional[str]]:
        """``(name, stored instruction)`` for *definition_id*, either one ``None``.

        Resolved the same way the failure notice does it (``get_task`` mirrors
        scheduled tasks only, so a watch needs the definition row), in ONE lookup
        because both fields feed the same display copy, and best-effort: a store that
        cannot answer must not break the run it is describing. ``agent_run`` has no
        definition and passes ``None``.

        The stored instruction is the definition's own prompt — a task's ``prompt``,
        a watch's ``message`` (which the row already falls back to ``prefix`` for) —
        so it is the user-authored text with none of the evidence a composed run
        prompt appends.
        """

        identifier = str(definition_id or "").strip()
        if not identifier:
            return None, None
        try:
            task = self.store.get_task(identifier)
            if task is not None:
                return (
                    str(getattr(task, "name", "") or "").strip() or None,
                    str(getattr(task, "prompt", "") or "").strip() or None,
                )
            watch = self.store.get_watch_definition(identifier) or {}
        except Exception:
            logger.debug("failed to resolve definition display fields for %s", identifier, exc_info=True)
            return None, None
        return (
            str(watch.get("name") or "").strip() or None,
            str(watch.get("message") or watch.get("prefix") or "").strip() or None,
        )

    def _resolve_target_context(self, target: ParsedSessionKey) -> Dict[str, Any]:
        platform = target.platform
        if platform not in self.controller.platform_settings_managers:
            # Virtual platform (avibe workbench): no per-platform settings manager
            # and no DM bindings — the scope_id IS the session/channel, and a
            # scheduled run is attributed to a synthetic "scheduled" user.
            return {"user_id": "scheduled", "channel_id": target.scope_id}
        settings_manager = self.controller.platform_settings_managers[platform]

        channel_id = target.scope_id
        user_id = "scheduled"
        if target.is_dm:
            user_id = target.scope_id
            bound_user = settings_manager.get_store().get_user(target.scope_id, platform=platform)
            if platform == "lark":
                dm_chat_id = getattr(bound_user, "dm_chat_id", "") if bound_user else ""
                if not dm_chat_id:
                    raise ValueError(f"lark user {target.scope_id} is missing dm_chat_id binding")
                channel_id = dm_chat_id
            elif bound_user and getattr(bound_user, "dm_chat_id", ""):
                channel_id = bound_user.dm_chat_id

        return {
            "user_id": user_id,
            "channel_id": channel_id,
        }

    def _resolve_delivery_target(
        self,
        *,
        session_target: ParsedSessionKey,
        post_to: Optional[str],
        deliver_key: Optional[str],
    ) -> ParsedSessionKey:
        if deliver_key:
            delivery_target = parse_session_key(deliver_key)
            if delivery_target.platform != session_target.platform:
                raise ValueError("--deliver-key must stay on the same platform as the session target")
            return delivery_target
        if post_to == "channel":
            return ParsedSessionKey(
                platform=session_target.platform,
                scope_type=session_target.scope_type,
                scope_id=session_target.scope_id,
                thread_id=None,
            )
        if post_to == "thread":
            if not session_target.thread_id:
                raise ValueError("--post-to thread requires a thread-bound session target or an explicit --deliver-key")
            return session_target
        return session_target

    def _supports_threaded_delivery(self, target: ParsedSessionKey) -> bool:
        getter = getattr(self.controller, "get_im_client_for_context", None)
        context = MessageContext(
            user_id=target.scope_id if target.is_dm else "scheduled",
            channel_id=target.scope_id,
            platform=target.platform,
            platform_specific={"platform": target.platform, "is_dm": target.is_dm},
        )
        if callable(getter):
            im_client = getter(context)
        else:
            im_client = getattr(self.controller, "im_client", None)
        if im_client is None:
            return False
        if target.is_dm:
            return bool(getattr(im_client, "should_use_thread_for_dm_session", lambda: False)())
        return bool(getattr(im_client, "should_use_thread_for_reply", lambda: False)())

    def _build_delivery_alias_strategy(
        self,
        *,
        session_target: ParsedSessionKey,
        delivery_target: ParsedSessionKey,
        session_context: Dict[str, Any],
        delivery_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        source_session_key = session_target.session_scope
        target_session_key = delivery_target.session_scope
        same_scope = source_session_key == target_session_key
        clear_provisional_source = session_target.thread_id is None and self._supports_threaded_delivery(session_target)

        if delivery_target.thread_id:
            alias_base = build_thread_session_anchor(
                delivery_target.platform,
                delivery_target.scope_id,
                delivery_target.thread_id,
            )
            source_alias_base = (
                build_thread_session_anchor(
                    session_target.platform,
                    session_target.scope_id,
                    session_target.thread_id,
                )
                if session_target.thread_id
                else None
            )
            if same_scope and alias_base == source_alias_base:
                return {"mode": "none"}
            return {
                "mode": "fixed_base",
                "session_key": target_session_key,
                "base_session_id": alias_base,
                "clear_source": clear_provisional_source,
            }

        if self._supports_threaded_delivery(delivery_target):
            return {
                "mode": "sent_message",
                "session_key": target_session_key,
                "clear_source": clear_provisional_source,
            }

        delivery_base_id = delivery_context["channel_id"]
        source_base_id = session_context["channel_id"]
        if same_scope and session_target.thread_id is None and delivery_base_id == source_base_id:
            return {"mode": "none"}
        return {
            "mode": "fixed_base",
            "session_key": target_session_key,
            "base_session_id": f"{delivery_target.platform}_{delivery_base_id}",
            "clear_source": clear_provisional_source,
        }

    @staticmethod
    def _build_message_id(*, execution_id: str, task_id: Optional[str], trigger_kind: str) -> str:
        if trigger_kind == "hook":
            return f"hook:{execution_id}"
        if trigger_kind == "watch":
            return f"watch:{task_id}:{execution_id}" if task_id else f"watch:{execution_id}"
        if trigger_kind == "webhook":
            return f"webhook:{task_id}:{execution_id}" if task_id else f"webhook:{execution_id}"
        if trigger_kind == "agent_run":
            return f"agent_run:{execution_id}"
        if task_id:
            return f"scheduled:{task_id}:{execution_id}"
        return f"scheduled:{execution_id}"
