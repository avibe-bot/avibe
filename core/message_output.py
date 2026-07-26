"""Message-output semantics shared by every agent backend.

The visible Message and the lifecycle event it may cause are deliberately
separate. Live runtime paths carry explicit lifecycle authority; one quarantined
dispatcher fallback preserves older callers that still use terminal ``result``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from core.run_settlement import SETTLED_BY_STOPPED


@dataclass(frozen=True)
class MessageOutput:
    """Lifecycle and hidden provenance for one user-visible agent output."""

    completes_turn: bool = False
    completes_run: bool | None = None
    detached: bool = False
    idempotency_key: str | None = None
    activity_id: str | None = None
    causation_id: str | None = None
    sequence: int | None = None
    run_id: str | None = None
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
            if trigger_kind == "agent_run"
            else ""
        )
        values: dict[str, Any] = {
            "turn_id": str(spec.get("turn_token") or "").strip() or None,
            "activity_id": self.activity_id,
            "run_id": self.run_id or inferred_run_id or None,
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
        activity_lineage = f"activity:{self.activity_id}" if self.activity_id else ""
        lineage = str(
            self.run_id
            or activity_lineage
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
