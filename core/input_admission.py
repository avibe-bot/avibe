"""One admission entry for every input that reaches an Avibe Session.

Every input source (the Web composer, IM, Harness triggers, Show annotations, a
failure retry, and an agent-initiated Turn) normalizes its input into one
``InputEnvelope`` and admits it here. The entry asks the admission policy for
the input's priority, records which policy decided and why on the Delivery's
admission history event, and persists the reserved Delivery.

Everything after that is unchanged and owned elsewhere: P0/P1/P3 admission in
``SessionTurnManager.deliver``, steering, the FIFO, segment merging, and the
dedupe identity each source supplies.

The policy is the seam for an agent-owned input firewall and a mailbox. Only the
default policy exists; it holds each source's released priority. Another policy
replaces it by implementing ``AdmissionPolicy``, or composes with it by
delegating to it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Protocol

from sqlalchemy.engine import Connection

from core.message_priority import (
    DeliveryIntent,
    DeliveryPriority,
    delivery_intent_for_trigger,
    normalize_delivery_intent,
    priority_for_delivery_intent,
)
from storage import message_deliveries


InputSource = Literal[
    "web_composer",
    "im",
    "harness",
    "show_annotation",
    "failure_retry",
    "agent_initiated",
]


@dataclass(frozen=True)
class InputSender:
    """Who sent the input.

    ``kind`` is the transcript author (``user`` or ``harness``); ``id`` and
    ``name`` are the identity the transcript shows for it.
    """

    kind: str
    id: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class InputProvenance:
    """Where the input came from."""

    source: InputSource
    platform: str = "avibe"
    #: The transcript ``source`` column (``user`` or ``harness``).
    origin: str = "user"
    native_message_id: str | None = None
    parent_native_message_id: str | None = None
    #: The Harness trigger kind (``scheduled``, ``watch``, ``agent_run``, ...).
    trigger: str | None = None


@dataclass(frozen=True)
class InputEnvelope:
    """One input, normalized, before any admission decision."""

    session_id: str
    scope_id: str | None
    sender: InputSender
    #: The Message type the input is recorded as (``user``, ``harness``,
    #: ``annotation``, ``agent_initiated``).
    kind: str
    provenance: InputProvenance
    #: The prompt the agent receives.
    dispatch_text: str = ""
    #: The text the transcript shows.
    text: str | None = None
    #: The Message content, attachments included.
    content: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    #: How a person produced it (``original``, ``quick_reply``, ...).
    message_kind: str | None = None
    #: An explicit request from the sender (``queue``, ``steer``, legacy
    #: ``send_now``), or ``None`` to take the source's default.
    requested_intent: str | None = None
    #: The backend the input is routed to, and the native subagent an IM input
    #: named with an explicit prefix.
    backend: str | None = None
    subagent: str | None = None

    @property
    def attachments(self) -> list[dict[str, Any]]:
        return list((self.content or {}).get("attachments") or [])

    def snapshot(self) -> dict[str, Any]:
        """The immutable Message candidate the Delivery holds."""

        return message_deliveries.message_snapshot(
            scope_id=self.scope_id,
            session_id=self.session_id,
            platform=self.provenance.platform,
            author=self.sender.kind,
            source=self.provenance.origin,
            message_type=self.kind,
            text=self.text,
            content=self.content,
            metadata=self.metadata,
            author_id=self.sender.id,
            author_name=self.sender.name,
            native_message_id=self.provenance.native_message_id,
            parent_native_message_id=self.provenance.parent_native_message_id,
            message_kind=self.message_kind,
        )


@dataclass(frozen=True)
class AdmissionDecision:
    intent: DeliveryIntent
    policy: str
    reason: str

    @property
    def priority(self) -> DeliveryPriority:
        return priority_for_delivery_intent(self.intent)

    def record(self) -> dict[str, str]:
        """Who decided and why, as recorded in the Delivery history."""

        return {"id": self.policy, "reason": self.reason}


class AdmissionPolicy(Protocol):
    def decide(self, envelope: InputEnvelope) -> AdmissionDecision: ...


class DefaultAdmissionPolicy:
    """Each source's released priority."""

    id = "default"

    def decide(self, envelope: InputEnvelope) -> AdmissionDecision:
        source = envelope.provenance.source
        if source == "harness":
            return self._decide_harness(envelope)
        if source == "im":
            if envelope.backend == "opencode" and envelope.subagent:
                # OpenCode subagents share the main Session anchor. They need a
                # fresh Turn so the persisted route can select the requested
                # native subagent instead of text-steering the active main Turn.
                return self._decision("queue", "opencode_subagent")
            return self._decision(delivery_intent_for_trigger("im"), "source:im")
        if source == "show_annotation":
            return self._decision(
                delivery_intent_for_trigger("show_annotation"),
                "source:show_annotation",
            )
        if source in {"web_composer", "failure_retry", "agent_initiated"}:
            return self._decision("queue", f"source:{source}")
        raise ValueError(f"unsupported input source: {source}")

    def _decide_harness(self, envelope: InputEnvelope) -> AdmissionDecision:
        requested = envelope.requested_intent
        if requested is not None:
            return self._decision(
                normalize_delivery_intent(requested),
                f"requested:{requested.strip().lower()}",
            )
        trigger = str(envelope.provenance.trigger or "").strip()
        if trigger:
            return self._decision(delivery_intent_for_trigger(trigger), f"trigger:{trigger}")
        return self._decision("steer", "harness_default")

    def _decision(self, intent: DeliveryIntent, reason: str) -> AdmissionDecision:
        return AdmissionDecision(intent=intent, policy=self.id, reason=reason)


DEFAULT_ADMISSION_POLICY: AdmissionPolicy = DefaultAdmissionPolicy()


@dataclass(frozen=True)
class AdmittedInput:
    delivery: dict[str, Any]
    decision: AdmissionDecision


def decide_admission(
    envelope: InputEnvelope,
    *,
    policy: AdmissionPolicy = DEFAULT_ADMISSION_POLICY,
) -> AdmissionDecision:
    return policy.decide(envelope)


def reserve_input(
    conn: Connection,
    envelope: InputEnvelope,
    decision: AdmissionDecision,
    *,
    delivery_id: str,
    dedupe_key: str | None = None,
    admission_context: Mapping[str, Any] | None = None,
    history: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist the reserved Delivery for a decided input in the caller's transaction.

    ``history`` replaces the admission event for a source that records its own
    (a failure retry's binding, an agent-initiated Turn's start); the decision's
    record is added to whichever event admits the Delivery.
    """

    event = (
        dict(history)
        if history is not None
        else {
            "kind": "admission",
            "priority": decision.priority,
            "state": "reserved",
            **({"context": dict(admission_context)} if admission_context else {}),
        }
    )
    event["policy"] = decision.record()
    return message_deliveries.insert_delivery(
        conn,
        delivery_id=delivery_id,
        session_id=envelope.session_id,
        priority=decision.priority,
        state="reserved",
        snapshot=envelope.snapshot(),
        dispatch_text=envelope.dispatch_text,
        dedupe_key=dedupe_key,
        history_event=event,
    )


def admit_input(
    conn: Connection,
    envelope: InputEnvelope,
    *,
    delivery_id: str,
    dedupe_key: str | None = None,
    admission_context: Mapping[str, Any] | None = None,
    history: Mapping[str, Any] | None = None,
    policy: AdmissionPolicy = DEFAULT_ADMISSION_POLICY,
) -> AdmittedInput:
    """Decide one input's priority and reserve its Delivery."""

    decision = decide_admission(envelope, policy=policy)
    delivery = reserve_input(
        conn,
        envelope,
        decision,
        delivery_id=delivery_id,
        dedupe_key=dedupe_key,
        admission_context=admission_context,
        history=history,
    )
    return AdmittedInput(delivery=delivery, decision=decision)
