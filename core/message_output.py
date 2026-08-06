"""Message-output semantics shared by every agent backend.

The visible Message and the lifecycle event it may cause are deliberately
separate. Live runtime paths carry explicit lifecycle authority; one quarantined
dispatcher fallback preserves older callers that still use terminal ``result``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from core.run_settlement import SETTLED_BY_BACKEND_REFRESH, SETTLED_BY_STOPPED

# Every trigger kind whose dispatch created an ``agent_runs`` row. Scheduled
# tasks, watches, webhooks, hooks and recovered Activities are all Harness runs
# and must reach the same result recorders as a direct ``vibe agent run``.
# Gating those recorders on ``agent_run`` alone is why every ``scheduled`` /
# ``watch`` row carries an empty ``result_text``.
HARNESS_TRIGGER_KINDS: frozenset[str] = frozenset(
    {"agent_run", "scheduled", "watch", "webhook", "hook", "activity_recovery"}
)

# The subset whose ``task_execution_id`` *is* the run id. ``activity_recovery``
# is deliberately excluded: it builds its context with a synthetic
# ``activity:<backend>:<id>`` execution id, and its real run ids travel on the
# Activity completion output instead. Reading its ``task_execution_id`` as a run
# id addresses a write to a row that cannot exist.
HARNESS_RUN_ID_TRIGGER_KINDS: frozenset[str] = HARNESS_TRIGGER_KINDS - {"activity_recovery"}

# ``platform_specific`` key carrying the Harness prompt that should be echoed into
# the turn's IM conversation. The turn pipeline stages it; ``AgentService`` emits it
# once the runtime turn gate is acquired, so a turn queued behind another turn cannot
# announce its prompt while that other turn is still working.
HARNESS_PROMPT_ECHO_SPEC_KEY = "harness_prompt_echo_text"

# The echo is awaited WITH the runtime turn gate held, so a slow or unreachable IM
# API would delay the Harness turn itself plus every turn queued on that gate — an
# adapter's own budget is far longer than a turn start should ever wait (Telegram
# allows 60s per request). Bounded like the status-bubble post that follows it
# (``MessageDispatcher.begin_status_bubble``); the echo is optional, the turn is not.
HARNESS_PROMPT_ECHO_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class MessageOutput:
    """Lifecycle and hidden provenance for one user-visible agent output."""

    completes_turn: bool = False
    completes_run: bool | None = None
    detached: bool = False
    idempotency_key: str | None = None
    native_message_id_aliases: tuple[str, ...] = ()
    activity_id: str | None = None
    activity_ids: tuple[str, ...] = ()
    activity_batch_id: str | None = None
    causation_id: str | None = None
    sequence: int | None = None
    run_id: str | None = None
    run_ids: tuple[str, ...] = ()
    requires_delivery_for_run_settlement: bool = False
    settled_by: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def settles_run(self) -> bool:
        """Run completion defaults to legacy Turn completion unless separated."""

        return self.completes_turn if self.completes_run is None else self.completes_run

    def provenance(self, context: Any) -> dict[str, Any]:
        spec = getattr(context, "platform_specific", None) or {}
        trigger_kind = str(spec.get("task_trigger_kind") or "").strip()
        inferred_run_id = (
            str(spec.get("task_execution_id") or "").strip()
            if trigger_kind in HARNESS_RUN_ID_TRIGGER_KINDS
            else ""
        )
        values: dict[str, Any] = {
            "turn_id": str(spec.get("turn_token") or "").strip() or None,
            "activity_id": self.activity_id,
            "activity_ids": list(self.activity_ids) or None,
            "activity_batch_id": self.activity_batch_id,
            "run_id": self.run_id or inferred_run_id or None,
            "run_ids": list(self.run_ids) or None,
            "causation_id": self.causation_id,
            "sequence": self.sequence,
            "output_id": self.idempotency_key,
            "detached": self.detached,
        }
        values.update(dict(self.metadata))
        return {key: value for key, value in values.items() if value is not None}

    def native_message_id(self, context: Any) -> str | None:
        """Stable persistence identity without exposing protocol text to users."""

        key = str(self.idempotency_key or "").strip()
        if not key:
            return None
        spec = getattr(context, "platform_specific", None) or {}
        target = spec.get("agent_session_target")
        backend = str(
            self.metadata.get("backend") or spec.get("vibe_agent_backend") or ""
        ).strip()
        if not backend and isinstance(target, dict):
            backend = str(target.get("agent_backend") or "").strip()
        activity_lineage = (
            f"activity-batch:{self.activity_batch_id}"
            if self.activity_batch_id
            else (f"activity:{self.activity_id}" if self.activity_id else "")
        )
        lineage = str(
            activity_lineage
            or self.run_id
            or spec.get("task_execution_id")
            or spec.get("agent_session_id")
            or spec.get("agent_runtime_turn_key")
            or "session"
        ).strip()
        return f"agent-output:{backend or 'unknown'}:{lineage}:{key}"


def output_for_message(message_type: str, output: MessageOutput | None) -> MessageOutput:
    """Normalize output semantics at the legacy dispatcher boundary.

    Live backend and shared-core paths provide explicit ``MessageOutput``. The
    result fallback remains only as a compatibility adapter for external callers
    while the visible Message role and lifecycle authority evolve separately.
    """

    if output is not None:
        return output
    if message_type == "result":
        return terminal_turn_output()
    return MessageOutput(completes_turn=False, completes_run=False)


def terminal_turn_output() -> MessageOutput:
    """Explicitly grant one output authority to settle its Turn and Run."""

    return MessageOutput(completes_turn=True, completes_run=True)


def terminal_output_for(request: Any) -> MessageOutput:
    """Use a request's explicit output policy or the terminal Turn default."""

    output = getattr(request, "output", None)
    return output if isinstance(output, MessageOutput) else terminal_turn_output()


def stop_output_for(request: Any) -> MessageOutput:
    """A user stop's synthetic terminal result: ends the Turn, does NOT own the Run.

    Every backend answers an acknowledged stop with an empty silent ``result`` so the
    dot settles to idle and the SSE waiter closes through the one outbound chokepoint.
    That output is the *turn's* release, not the *run's* result: nobody produced an
    answer, the user called the work off. Sending it with the terminal-turn default
    (``completes_run=True``) made the dispatcher record the run ``succeeded`` from an
    empty body — and, because the terminal write lands before the stop's own guarded
    write, first-writer-wins meant a user-ended run was reported as a success.

    So: ``completes_run=False`` keeps the empty result out of the run's terminal
    state, while ``settled_by`` names the reason on the turn sink so the settlement
    lanes reach the writer that already knows a stop is ``canceled`` with
    ``interrupt_reason=stopped`` (``SETTLEMENT_TERMINAL_STATUS``). Without the reason,
    "does not settle the run" would read as ``turn_only_result`` — the Activity case,
    where somebody else genuinely owns the row — and the run would sit ``running``
    until the staleness sweep called it ``orphaned``.

    Built with ``replace`` so a request carrying its own output policy (Activity
    lineage, an explicit ``run_id``) keeps it; only the lifecycle is overridden.
    """

    return replace(
        terminal_output_for(request),
        completes_turn=True,
        completes_run=False,
        settled_by=SETTLED_BY_STOPPED,
    )


def contained_teardown_output_for(request: Any) -> MessageOutput:
    """A service-initiated backend teardown's terminal result: infrastructure, not fault.

    The service killed this backend runtime itself -- idle eviction, duplicate reap,
    a rolling ``agents.*`` reconciliation. #1202 taught the Claude paths to recognize
    that signal and stop reporting it as a user-visible backend error or a Model Hub
    source-health failure. It did not change what the emit that follows still says:
    an ordinary ``result`` with ``is_error=True``, which the dispatcher reads as a
    FAILED turn, IM silent-terminal evidence stamped ``failed``, and an
    ``agent_runs`` row terminalized from an empty body. Suppressing the bubble while
    writing that provenance means the durable record disagrees with the truth the
    classification already established.

    Same shape as ``stop_output_for``, different reason. ``completes_run=False``
    keeps the empty body out of ``_record_agent_run_terminal_result``, and
    ``settled_by`` routes the release through the settlement lane instead, where
    ``SETTLEMENT_TERMINAL_STATUS`` already maps ``backend_refresh`` to ``failed``
    with ``interrupt_reason=backend_refresh``. That preserves invariant 2 of
    ``docs/plans/harness-run-reliability.md`` -- an infrastructure interruption is
    ``failed`` with a STRUCTURED cause, never silently swallowed -- while making the
    cause distinguishable from a backend that actually broke.

    ``backend_refresh`` rather than ``interrupted`` because it already names this
    exact boundary: a live Agent runtime retired inside an otherwise healthy
    service. Its callers emit ``is_error=False``; the failure is expressed by the
    settlement, not by the dot.

    Nothing here is Claude-specific. Any backend whose cleanup can classify its own
    teardown gets the same semantics by emitting through this factory.
    """

    return replace(
        terminal_output_for(request),
        completes_turn=True,
        completes_run=False,
        settled_by=SETTLED_BY_BACKEND_REFRESH,
    )
